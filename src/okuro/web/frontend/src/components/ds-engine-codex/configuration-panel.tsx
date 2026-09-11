import { useEffect, useState } from "react";
import { ChevronDown, Copy, RotateCcw, Save, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import type {
  BrandJson,
  Flag,
  FontFamily,
  ResolvedModel,
} from "@/components/design-engine/types";

interface ConfigurationPanelProps {
  brand: BrandJson;
  model: ResolvedModel;
  families: FontFamily[];
  appearance: "light" | "dark";
  rung: string;
  scaled: boolean;
  editable: boolean;
  dirty: boolean;
  saving: boolean;
  onAppearance: (value: "light" | "dark") => void;
  onRung: (value: string) => void;
  onScaled: (value: boolean) => void;
  onChange: (brand: BrandJson) => void;
  onReset: () => void;
  onSave: () => void;
  onDuplicate: (id: string) => void;
}

const RUNGS = ["XXL", "XL", "L", "M", "S", "XS", "XXS", "XXXS"];
const FONT_SLOTS = [
  { key: "slot_1", ordinal: 1 },
  { key: "slot_2", ordinal: 2 },
  { key: "slot_3", ordinal: 3 },
  { key: "slot_4", ordinal: 4 },
] as const;
const TYPE_BANDS = ["titles", "headings", "leads", "paragraphs"] as const;

/**
 * WHICH AUTHORED FIELD A FLAG'S `where` NAMES.
 *
 * The engine addresses a signal by its ROLE — `signals.error` — because a role
 * is what the rule is about; a brand authors the same colour under its HUE name,
 * `signals.red`, because that is what it picked. `api.py:_flag` documents `where`
 * as the address that "reaches the rail's field" and the rail was reaching for
 * nothing: it rendered `model.flags.length` in three places and the messages in
 * none, so six sentences carrying a WCAG ratio and a remedy arrived as the
 * string "2 findings".
 *
 * The pairs are the engine's own (`schema.Signals.by_role`), named once here
 * because this is the only place the two vocabularies meet.
 */
const WHERE_ALIASES: Record<string, string> = {
  "signals.error": "signals.red",
  "signals.success": "signals.green",
  "signals.info": "signals.blue",
  "signals.warning": "signals.pink",
};

/**
 * The authored paths this panel actually renders a control for.
 *
 * Declared beside the fields rather than inferred, because "is there a field for
 * this address" is the question that decides whether a finding is placed or
 * listed — and a lookup that guesses would place a flag under a control that
 * does not write that value.
 */
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

/** Every field path a flag's `where` reaches, one flag possibly reaching two. */
function addressesOf(flag: Flag): string[] {
  const where = WHERE_ALIASES[flag.where] ?? flag.where;
  // `brand.on_light/on_dark` is the engine's way of saying "both poles"; it is
  // an address to two fields, not a field called `on_light/on_dark`.
  const [head, tail] = where.split("/") as [string, string | undefined];
  if (!tail) return [head];
  const parent = head.slice(0, head.lastIndexOf("."));
  return [head, `${parent}.${tail}`];
}

/** A dot per severity, so the tone is visible before the sentence is read. */
const SEVERITY_TONE: Record<string, string> = {
  "must-fix": "var(--color-status-error)",
  decide: "var(--color-status-warning)",
  note: "var(--color-status-info)",
};

/**
 * THE SHADE, AS A CONTROL RATHER THAN A READING.
 *
 * "light pole and dark pole can be overridden by user" is not a feature request:
 * `shade_light` / `shade_dark` are authored fields of `BrandColours`, and
 * `Brand._materialise` turns each into `on_{pole}` on every validation. The API
 * has been shipping `shade`, `proposed_shade`, `proposed_value`, `toward` and
 * `reason` per pole precisely so a slider can seed itself from the engine's own
 * proposal — and the panel rendered a read-only chip showing `0.39`.
 *
 * THE OWNER'S COLOUR RULE IS WHAT MAKES THIS SAFE, and it is why the control
 * writes an AMOUNT and never a colour: "color calculation can never be based on
 * a calculation of another color!" A shade is an answer about ONE colour, so
 * whenever the canonical moves the engine recomputes `on_{pole}` from the NEW
 * canonical through the single recipe. Nothing is carried across.
 *
 * NULL IS A REAL VALUE HERE, not a missing one: it means "no amount authored,
 * use the proposal". Reset puts it back, so the field can always return to
 * whatever the engine would have chosen.
 */
function ShadeField({ pole, value, resolved, onChange }: {
  pole: "light" | "dark";
  value: number | null | undefined;
  resolved: { value: string; proposed_shade: number; reason: string };
  onChange: (amount: number | null) => void;
}) {
  const amount = value ?? resolved.proposed_shade;
  const authored = value !== null && value !== undefined;
  return (
    <label className="dsc-shade-field">
      <span className="dsc-shade-head">
        <i style={{ background: resolved.value }} />
        <b className="ds-n7 ds-leads">{pole === "light" ? "Light pole" : "Dark pole"}</b>
        <output className="ds-n8 ds-paragraphs">{amount.toFixed(2)}</output>
      </span>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={amount}
        aria-label={`${pole} pole shade amount`}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <small className="ds-n8 ds-paragraphs">{resolved.reason}</small>
      {authored && (
        <button type="button" className="ds-n8 ds-leads" onClick={() => onChange(null)}>
          Back to the engine's {resolved.proposed_shade.toFixed(2)}
        </button>
      )}
    </label>
  );
}

/**
 * The engine's findings for one field, rendered where the value is typed.
 *
 * NEVER AN ERROR, ALWAYS A HINT — the owner: "Never error -> hint. Users need to
 * be able to configure what they want." The engine already agrees: a flag comes
 * back ALONGSIDE a fully resolved model, never instead of one, so `must-fix`
 * names how bad a finding is and not whether the value was accepted. The rail
 * was the only place in the chain that turned a hint into a refusal.
 */
function FieldFlags({ flags, id }: { flags: Flag[]; id: string }) {
  if (!flags.length) return null;
  return (
    <ul className="dsc-field-flags ds-n8 ds-paragraphs" id={id}>
      {flags.map((flag) => (
        <li key={`${flag.code}-${flag.where}`} data-severity={flag.severity ?? "note"}>
          <i style={{ background: SEVERITY_TONE[flag.severity ?? "note"] }} />
          <span>{flag.message}</span>
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

function PanelSection({ title, meta, open = false, children }: { title: string; meta?: string; open?: boolean; children: React.ReactNode }) {
  return <details className="dsc-panel-section" open={open}><summary><span className="ds-n7 ds-leads">{title}</span>{meta && <small className="ds-n8 ds-paragraphs">{meta}</small>}<ChevronDown /></summary><div className="dsc-panel-section-body">{children}</div></details>;
}

function Field({ label, hint, flags, children }: { label: string; hint?: string; flags?: Flag[]; children: React.ReactNode }) {
  const id = `dsc-flags-${label.replace(/\W+/g, "-").toLowerCase()}`;
  return <label className="dsc-panel-field"><span className="ds-n7 ds-leads">{label}</span>{children}{hint && <small className="ds-n8 ds-paragraphs">{hint}</small>}{flags && <FieldFlags flags={flags} id={id} />}</label>;
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
function ColorField({ label, value, flags = [], onChange }: { label: string; value: string; flags?: Flag[]; onChange: (value: string) => void }) {
  const id = `dsc-flags-${label.replace(/\W+/g, "-").toLowerCase()}`;
  const invalid = flags.some((flag) => flag.severity === "must-fix");
  return <Field label={label} flags={flags}><span className="dsc-color-field"><input type="color" value={value} onChange={(event) => onChange(event.target.value)} aria-label={`${label} color`} /><Input value={value} aria-invalid={invalid || undefined} aria-describedby={flags.length ? id : undefined} onChange={(event) => onChange(event.target.value)} /></span></Field>;
}

function NumberField({ label, value, min, max, step = 1, onChange }: { label: string; value: number; min?: number; max?: number; step?: number; onChange: (value: number) => void }) {
  return <Field label={label}><Input type="number" value={value} min={min} max={max} step={step} onChange={(event) => onChange(Number(event.target.value))} /></Field>;
}

export function ConfigurationPanel(props: ConfigurationPanelProps) {
  const { brand, model, families, onChange } = props;
  const [copyId, setCopyId] = useState(`${brand.id}-custom`);
  const set = (path: string, value: unknown) => onChange(setPath(brand, path, value));

  // EVERY FLAG IS PLACED, AND THE ONES THAT ARE NOT ARE STILL SHOWN. A `where`
  // this panel has no field for must never be dropped silently — that is how a
  // finding becomes invisible the day the engine learns a new address. The
  // leftovers render as a list at the foot of the panel instead.
  const placed = new Map<string, Flag[]>();
  const unplaced: Flag[] = [];
  for (const flag of model.flags) {
    const addresses = addressesOf(flag).filter((a) => PANEL_FIELDS.has(a));
    if (!addresses.length) { unplaced.push(flag); continue; }
    for (const address of addresses) {
      placed.set(address, [...(placed.get(address) ?? []), flag]);
    }
  }
  const flagsAt = (path: string) => placed.get(path) ?? [];
  const availableFaces = Object.entries(brand.font.faces ?? {}).sort(([, a], [, b]) => b - a);
  const slotName = (ordinal: number) => {
    const descriptor = brand.font[`slot_${ordinal}` as keyof typeof brand.font];
    const weight = numericWeight(String(descriptor), brand.font.faces);
    return `Slot ${ordinal} — ${descriptor}${weight === null ? "" : ` · ${weight}`}`;
  };

  useEffect(() => setCopyId(`${brand.id}-custom`), [brand.id]);

  return <div className="dsc-config-content ds-n7 ds-paragraphs">
    <div className="dsc-panel-actions">
      <Button size="sm" variant="ghost" onClick={props.onReset} disabled={!props.dirty}><RotateCcw /> Reset</Button>
      {props.editable
        ? <Button size="sm" onClick={props.onSave} disabled={!props.dirty || props.saving}><Save />{props.saving ? "Saving…" : "Save system"}</Button>
        : <span className="dsc-protected ds-n8 ds-leads">Protected source</span>}
    </div>

    <PanelSection title="Preview" meta="not saved" open>
      <div className="dsc-panel-field"><span className="ds-n7 ds-leads">Appearance</span><div className="dsc-panel-segments">{(["light", "dark"] as const).map((value) => <button className="ds-n7 ds-paragraphs" key={value} type="button" data-active={props.appearance === value || undefined} onClick={() => props.onAppearance(value)}>{value}</button>)}</div></div>
      <div className="dsc-panel-field"><span className="ds-n7 ds-leads">Root size rung</span><select className="ds-n7 ds-paragraphs" value={props.rung} onChange={(event) => props.onRung(event.target.value)}>{RUNGS.map((value) => <option key={value}>{value}</option>)}</select><small className="ds-n8 ds-paragraphs">Inherited by text and components in the full page.</small></div>
      <div className="dsc-panel-switch"><span><b className="ds-n7 ds-leads">Downscale context</b><small className="ds-n8 ds-paragraphs">Text shifts one rung; components shift three.</small></span><Switch checked={props.scaled} onCheckedChange={props.onScaled} /></div>
    </PanelSection>

    <PanelSection title="Identity" meta="authored" open>
      <ColorField label="Canonical brand" value={brand.brand.canonical} flags={flagsAt("brand.canonical")} onChange={(value) => set("brand.canonical", value)} />
      <div className="dsc-panel-switch"><span><b className="ds-n7 ds-leads">Two-color mode</b><small className="ds-n8 ds-paragraphs">Independent light and dark pole colors.</small></span><Switch checked={brand.brand.two_colour_mode} onCheckedChange={(value) => set("brand.two_colour_mode", value)} /></div>
      {brand.brand.two_colour_mode
        ? <><ColorField label="On light" value={brand.brand.on_light ?? brand.brand.canonical} flags={flagsAt("brand.on_light")} onChange={(value) => set("brand.on_light", value)} /><ColorField label="On dark" value={brand.brand.on_dark ?? brand.brand.canonical} flags={flagsAt("brand.on_dark")} onChange={(value) => set("brand.on_dark", value)} /></>
        : <div className="dsc-resolved-poles">{(["light", "dark"] as const).map((pole) => {
            const p = model.brand_colours.poles[pole];
            return <ShadeField key={pole} pole={pole} value={brand.brand[pole === "light" ? "shade_light" : "shade_dark"]} resolved={p} onChange={(amount) => set(`brand.shade_${pole}`, amount)} />;
          })}</div>}
    </PanelSection>

    <PanelSection title="Color roles" meta="6 authored">
      <div className="dsc-panel-pair"><ColorField label="Black" value={brand.neutrals.black} flags={flagsAt("neutrals.black")} onChange={(value) => set("neutrals.black", value)} /><ColorField label="White" value={brand.neutrals.white} flags={flagsAt("neutrals.white")} onChange={(value) => set("neutrals.white", value)} /></div>
      <div className="dsc-panel-pair"><ColorField label="Error" value={brand.signals.red} flags={flagsAt("signals.red")} onChange={(value) => set("signals.red", value)} /><ColorField label="Success" value={brand.signals.green} flags={flagsAt("signals.green")} onChange={(value) => set("signals.green", value)} /></div>
      <div className="dsc-panel-pair"><ColorField label="Info" value={brand.signals.blue} flags={flagsAt("signals.blue")} onChange={(value) => set("signals.blue", value)} /><ColorField label="Warning" value={brand.signals.pink} flags={flagsAt("signals.pink")} onChange={(value) => set("signals.pink", value)} /></div>
      <p className="dsc-panel-note">The engine picks foreground ink for every signal from the same contrast threshold.</p>
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
      <div className="dsc-panel-pair"><Field label="Desktop rung"><select value={brand.defaults.rung.desktop} onChange={(event) => set("defaults.rung.desktop", event.target.value)}>{RUNGS.map((value) => <option key={value}>{value}</option>)}</select></Field><Field label="Mobile rung"><select value={brand.defaults.rung.mobile} onChange={(event) => set("defaults.rung.mobile", event.target.value)}>{RUNGS.map((value) => <option key={value}>{value}</option>)}</select></Field></div>
      <div className="dsc-type-subsection">
        <div className="dsc-type-subsection-head"><span className="ds-n8 ds-leads">WEIGHT SLOTS</span><small className="ds-n8 ds-paragraphs">Each slot is named by its selected font face.</small></div>
        <div className="dsc-font-slots">
          {FONT_SLOTS.map((slot) => {
            const descriptor = brand.font[slot.key];
            const weight = numericWeight(descriptor, brand.font.faces);
            return <label className="dsc-font-slot" key={slot.key}>
              <span><b className="ds-n7 ds-leads">Slot {slot.ordinal} — {descriptor}</b><small className="ds-n8 ds-paragraphs">Assigned font face{weight === null ? "" : ` · CSS ${weight}`}</small></span>
              {availableFaces.length
                ? <select className="ds-n7 ds-paragraphs" value={descriptor} onChange={(event) => set(`font.${slot.key}`, event.target.value)}>{availableFaces.map(([name, value]) => <option key={name} value={name}>{name} · {value}</option>)}</select>
                : <Input value={descriptor} aria-label={`Slot ${slot.ordinal} face name`} onChange={(event) => set(`font.${slot.key}`, event.target.value)} />}
            </label>;
          })}
        </div>
      </div>
      <div className="dsc-type-subsection">
        <div className="dsc-type-subsection-head"><span className="ds-n8 ds-leads">ROLE ASSIGNMENT</span><small className="ds-n8 ds-paragraphs">Each role selects a named slot and its case.</small></div>
        {TYPE_BANDS.map((role) => <div className="dsc-role-row" key={role}><span className="ds-n7 ds-paragraphs">{role}</span><select className="ds-n8 ds-paragraphs" aria-label={`${role} weight slot`} value={String(brand.weights[role])} onChange={(event) => set(`weights.${role}`, Number(event.target.value))}>{FONT_SLOTS.map((slot) => <option key={slot.ordinal} value={slot.ordinal}>{slotName(slot.ordinal)}</option>)}</select><Switch checked={brand.case[role]} onCheckedChange={(value) => set(`case.${role}`, value)} aria-label={`${role} uppercase`} /></div>)}
      </div>
      <p className="dsc-panel-note">8 rungs × 21 text roles form the mother table. Components use 9 rungs × 16 roles; frame context selects the row.</p>
    </PanelSection>

    <PanelSection title="Geometry" meta="borders · radius · shadows">
      <div className="dsc-panel-pair"><NumberField label="Radius base" value={brand.radius.base} min={1} onChange={(value) => set("radius.base", value)} /><NumberField label="Border opacity" value={brand.border.opacity} min={0} max={1} step={.05} onChange={(value) => set("border.opacity", value)} /></div>
      <div className="dsc-panel-triple">{(["s_factor", "m_factor", "l_factor"] as const).map((key) => <NumberField key={key} label={key.replace("_factor", "").toUpperCase()} value={brand.radius[key]} min={0} step={.25} onChange={(value) => set(`radius.${key}`, value)} />)}</div>
      <div className="dsc-panel-pair"><NumberField label="Solid shadow" value={brand.shadow_anchors.solid} step={.05} onChange={(value) => set("shadow_anchors.solid", value)} /><NumberField label="Blurred shadow" value={brand.shadow_anchors.blurred} step={.05} onChange={(value) => set("shadow_anchors.blurred", value)} /></div>
    </PanelSection>

    <PanelSection title="Motion" meta={`${Object.keys(brand.motion.speeds).length} speeds · ${Object.keys(brand.motion.curves).length} curves`}>
      <Field label="Default speed"><select value={brand.motion.default_speed} onChange={(event) => set("motion.default_speed", event.target.value)}>{Object.keys(brand.motion.speeds).map((value) => <option key={value}>{value}</option>)}</select></Field>
      <Field label="Default curve"><select value={brand.motion.default_curve} onChange={(event) => set("motion.default_curve", event.target.value)}>{Object.keys(brand.motion.curves).map((value) => <option key={value}>{value}</option>)}</select></Field>
      <Field label="Signature"><Input value={brand.motion.signature ?? ""} placeholder="None" onChange={(event) => set("motion.signature", event.target.value || null)} /></Field>
    </PanelSection>

    <PanelSection title="System logic" meta={model.flags.length ? `${model.flags.length} findings` : "no findings"}>
      {unplaced.length > 0 && <ul className="dsc-field-flags ds-n8 ds-paragraphs">
        {unplaced.map((flag) => <li key={`${flag.code}-${flag.where}`} data-severity={flag.severity ?? "note"}><i style={{ background: SEVERITY_TONE[flag.severity ?? "note"] }} /><span><b>{flag.where}</b> — {flag.message}</span></li>)}
      </ul>}
      <dl className="dsc-provenance"><div><dt>Authored</dt><dd>{model.authored.length} inputs</dd></div><div><dt>Resolved</dt><dd>{model.grounds.length} grounds</dd></div><div><dt>Threshold</dt><dd>{model.threshold}</dd></div><div><dt>Generated</dt><dd>{model.requests.length} requests</dd></div><div><dt>Type table</dt><dd>{Object.keys(model.sizes.text_rungs).length} rungs</dd></div></dl>
      <ol className="dsc-stage-list">{["Authored identity", "Measured polarity", "Generated vocabulary", "Inherited frame", "Consumed component"].map((label, index) => <li key={label}><span>{index + 1}</span>{label}</li>)}</ol>
    </PanelSection>

    {!props.editable && <div className="dsc-duplicate"><span><Sparkles />Create an editable branch</span><p>Shipped systems stay immutable. Your current draft can be saved as a user system.</p><Input value={copyId} onChange={(event) => setCopyId(event.target.value)} /><Button onClick={() => props.onDuplicate(copyId)} disabled={!copyId.trim() || props.saving}><Copy />{props.saving ? "Creating…" : "Duplicate draft"}</Button></div>}
  </div>;
}
