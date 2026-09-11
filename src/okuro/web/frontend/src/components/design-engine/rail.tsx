/**
 * CONFIGURE — eight decisions, then everything else, behind one level.
 *
 * The register's rule 12 lists the authored set exactly, and this rail is still
 * that list. What changed is that it is no longer a FLAT list: 137 controls in
 * one column is 5.7 screens of scrolling, which is why "a lot of settings are
 * not obvious" and why nothing on it could be judged at a glance.
 *
 * EIGHT PRIMARY DECISIONS. Radix ships six, shadcn ten. Each one shows its
 * RESOLVED VALUE on the row — a rail that only showed controls makes a reader
 * open each one to find out what it currently says — and owns exactly one help
 * slot.
 *
 * THEN `Advanced`: seven collapses, ONE LEVEL, closed. Two hard rules:
 *
 *   1. Nothing needed to JUDGE the configuration lives inside a closed
 *      collapse. A group holding a flagged slot opens itself, and its header
 *      carries the severity glyph even when closed.
 *   2. One level. No nesting. `Type Sizes` opens to a rung selector and 21
 *      `PxField`s and stops there.
 *
 * RHYTHM: f2 (16px) between two controls of one group, f4 (32px) plus a
 * hairline between two groups. The ratio is the rule, not the numbers —
 * measured before, both were 11-12px, which is what "the arrangement is
 * catastrophic" is measuring.
 *
 * `FlagNotes` IS GONE. Advice comes from `advice.tsx` through each field's own
 * help slot, once. It used to render inside `BrandColourSection` AND inside
 * `FieldShell` for the same slot, which is two of the six copies of three
 * findings the page shipped.
 */

import { Fragment, useMemo, useState } from "react";
import { BASE } from "./scale";
import { assignFaceToBand, facesOfBands } from "./weights";
import { BrandColourSection } from "./brand-colour";
import {
  ColourField,
  NumberField,
  PxField,
  SegmentedField,
  SelectField,
  SwatchRow,
} from "./fields";
import { severityOf } from "./severity";
import type { AdviceAction } from "./severity";
import type {
  AdditionalColour,
  BrandColourModel,
  BrandJson,
  Flag,
  FontFamily,
} from "./types";

/**
 * The eight text rungs, as options for the two DEFAULT-RUNG dropdowns.
 *
 * His ruling of 2026-08-18 locked the mother table and left a brand one sizing
 * choice: which row of it to open on. So the rail no longer needs the band shape
 * — `TEXT_BANDS` went with the 21 `PxField`s it laid out — and needs only the
 * rung NAMES. They are stated here rather than read off `brand.sizes` because
 * they are the ENGINE's ladder now, identical for every brand, and the page suite
 * asserts this list against `model.sizes.text_rungs`.
 */
const RUNG_OPTIONS = ["XXL", "XL", "L", "M", "S", "XS", "XXS", "XXXS"].map((rung) => ({
  value: rung,
  label: rung,
}));

/* -------------------------------------------------------------- plumbing */

/** Immutable set at a dotted path. The brand is nested; edits are not. */
export function setPath<T>(source: T, path: string, value: unknown): T {
  const keys = path.split(".");
  const clone = (node: any, index: number): any => {
    const key = keys[index] as string;
    const next =
      index === keys.length - 1 ? value : clone(node?.[key] ?? {}, index + 1);
    if (Array.isArray(node)) {
      const copy = node.slice();
      copy[Number(key)] = next;
      return copy;
    }
    return { ...node, [key]: next };
  };
  return clone(source, 0) as T;
}

export function getPath(source: unknown, path: string): unknown {
  return path.split(".").reduce<any>((node, key) => node?.[key], source);
}

/**
 * Every authored slot a flag's `where` names.
 *
 * A `where` may name SEVERAL slots at once — `brand.on_light/on_dark` is ONE
 * finding about TWO fields, because "these two hold the same colour" is not a
 * statement about either one alone. Exact-matching the whole string sent that
 * flag to neither field: invisible in the rail while sitting in the payload.
 * The group prefix is carried from the first name onto the rest.
 */
export function slotsNamedBy(where: string): string[] {
  const parts = where.split("/");
  const head = parts[0] ?? "";
  const dot = head.lastIndexOf(".");
  const group = dot > 0 ? head.slice(0, dot) : "";
  return parts.map((part, i) =>
    i === 0 || !group || part.includes(".") ? part : `${group}.${part}`,
  );
}

/* ----------------------------------------------------------------- groups */

/** A primary decision. `data-group` is what the gate counts and measures. */
function Group({
  name,
  children,
}: {
  name: string;
  children: React.ReactNode;
}) {
  return (
    <section data-group={name} className="ce-stack">
      {children}
    </section>
  );
}

/**
 * One `Advanced` topic. One level, closed, and it opens itself when flagged.
 *
 * The header carries the severity glyph EVEN WHEN CLOSED, so a reader scanning
 * a shut rail can still see that something inside needs an answer — which is
 * the whole point of rule 1 above.
 */
function Collapse({
  title,
  flags,
  children,
}: {
  title: string;
  flags: Flag[];
  children: React.ReactNode;
}) {
  const worst = flags
    .slice()
    .sort((a, b) => rank(severityOf(b)) - rank(severityOf(a)))[0];
  const [open, setOpen] = useState(Boolean(worst));

  return (
    <div data-collapse={title} data-open={open || undefined}>
      <button
        type="button"
        className="ce-row"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        style={{
          width: "100%",
          minHeight: 32,
          gap: 8,
          background: "transparent",
          border: 0,
          padding: 0,
          color: "inherit",
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        <span aria-hidden className="ce-body ce-2" style={{ width: 12 }}>
          {open ? "▾" : "▸"}
        </span>
        <span className="ce-h2">{title}</span>
        {worst && (
          <span
            aria-hidden
            data-severity={severityOf(worst)}
            className="ce-body"
            style={{ marginLeft: "auto", color: "var(--ce-severity)" }}
          >
            {severityOf(worst) === "must-fix" ? "▲" : "◆"}
          </span>
        )}
      </button>
      {open && (
        <div className="ce-stack" style={{ marginTop: 16 }}>
          {children}
        </div>
      )}
    </div>
  );
}

function rank(severity: string): number {
  return severity === "must-fix" ? 3 : severity === "decide" ? 2 : 1;
}

/* ------------------------------------------------------------------ rail */

/** The weight quads a brand is likely to want, named. */
const WEIGHT_PRESETS: { value: string; label: string }[] = [
  /* SHORT ENOUGH TO FIT THE TRIGGER. Measured: the shipped labels rendered at
     260px inside a 229px trigger with `overflow: hidden` and `text-overflow:
     clip`, so the value was cut mid-word and ran under its own chevron. */
  { value: "1/1/2/3", label: "1 / 1 / 2 / 3 · heaviest" },
  { value: "1/2/3/4", label: "1 / 2 / 3 / 4 · one each" },
  { value: "2/2/3/4", label: "2 / 2 / 3 / 4 · softer" },
];

const SLOT_OPTIONS = [1, 2, 3, 4].map((n) => ({
  value: String(n),
  label: `slot ${n}`,
}));

/** The two states of a band's case toggle, labelled with what they render as. */
const CASE_OPTIONS = [
  { value: "none", label: "As written" },
  { value: "uppercase", label: "UPPERCASE" },
];

/** The four signals: the AUTHORED slot name, and the role it plays. */
const SIGNAL_ROLES = [
  ["red", "Error"],
  ["green", "Success"],
  ["blue", "Info"],
  ["pink", "Warning"],
] as const;

/** slot -> role, because the override is keyed by ROLE and the colour by slot. */
const SIGNAL_ROLE_OF: Record<string, string> = {
  red: "error",
  green: "success",
  blue: "info",
  pink: "warning",
};

/**
 * ONE SIGNAL'S INK — computed, or authored, with both numbers visible.
 *
 * His ruling of 2026-08-18: the override is "flagged-not-rejected with the
 * contrast readout shown ... surface WCAG numbers for both candidates next to the
 * override control so the decision is informed". So this is not a colour picker:
 * rule 13 allows exactly two inks — the brand's own black and its own white — and
 * the choice between them is what the control offers, each with the ratio it
 * actually achieves on that signal.
 *
 * `Computed` is a real option rather than a reset link, because "accept what the
 * system says" is the default and a reader should be able to see it selected. The
 * engine, not this file, decides which of the two is the computed one.
 */
function SignalInkChoice({
  role,
  ink,
  onChange,
}: {
  role: string;
  ink: NonNullable<BrandColourModel["signals"]>[string];
  onChange: (value: string | null) => void;
}) {
  const options: { value: string | null; label: string; contrast: number }[] = [
    { value: null, label: "Computed", contrast: ink.computed_contrast },
    {
      value: ink.alternative,
      label: ink.alternative.toLowerCase() === ink.computed.toLowerCase()
        ? "Same"
        : "Override",
      contrast: ink.alternative_contrast,
    },
  ];

  return (
    <div
      className="ce-row"
      data-signal-foreground={role}
      role="radiogroup"
      aria-label={`${role} foreground`}
      style={{ gap: 8, flexWrap: "wrap" }}
    >
      {options.map((option) => {
        const active =
          option.value === null ? ink.authored === null : ink.authored === option.value;
        const passes = option.contrast >= ink.aa;
        return (
          <button
            key={option.label}
            type="button"
            role="radio"
            className="ce-chip"
            aria-checked={active}
            data-ink={option.value ?? "computed"}
            /* THE NUMBER IS THE LABEL. A ratio the reader has to hover for is the
               affordance this page deleted by name. */
            onClick={() => onChange(option.value)}
          >
            <span
              aria-hidden
              className="ce-swatch"
              style={{
                background: ink.background,
                color: option.value ?? ink.computed,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 10,
              }}
            >
              Aa
            </span>
            {option.label} · {option.contrast.toFixed(2)} : 1
            {/* NOT COLOUR ALONE, per the in-line-alert rule: the glyph carries
                pass/fail as well as the number. */}
            <span aria-hidden>{passes ? "✓" : "▲"}</span>
          </button>
        );
      })}
      <span className="ce-micro ce-2">AA needs {ink.aa} : 1</span>
    </div>
  );
}

export function AuthoringRail({
  brand,
  families,
  flags,
  colours,
  additional,
  onChange,
  onReveal,
  onAction,
}: {
  brand: BrandJson;
  families: FontFamily[];
  flags: Flag[];
  /** The engine's answer about the brand colour: mode, shades, proposals. */
  colours: BrandColourModel;
  /** The additional colours WITH their computed foregrounds. */
  additional: AdditionalColour[];
  onChange: (next: BrandJson) => void;
  onReveal: (slot: string) => void;
  onAction: (action: AdviceAction, flag: Flag) => void;
}) {
  const set = (path: string, value: unknown) => onChange(setPath(brand, path, value));

  const flagsFor = (where: string) =>
    flags.filter((flag) => slotsNamedBy(flag.where).includes(where));

  /** Every flag whose `where` starts with one of these prefixes. */
  const flagsUnder = (...prefixes: string[]) =>
    flags.filter((flag) =>
      slotsNamedBy(flag.where).some((slot) =>
        prefixes.some((p) => slot === p || slot.startsWith(`${p}.`)),
      ),
    );

  const [signalsOpen, setSignalsOpen] = useState(false);
  const [neutralsOpen, setNeutralsOpen] = useState(false);
  const [weightsOpen, setWeightsOpen] = useState(false);

  /* A weight dropdown may only offer faces the family REALLY ships. A family
     whose measured faces are unknown offers nothing, and the field falls back
     to free text rather than inventing nine weights. */
  const faces = useMemo(() => {
    const family = families.find((f) => f.family === brand.font.family);
    return family?.faces ?? brand.font.faces ?? null;
  }, [families, brand.font.family, brand.font.faces]);

  const faceOptions = useMemo(
    () =>
      faces
        ? Object.entries(faces)
            .sort((a, b) => b[1] - a[1])
            .map(([descriptor, weight]) => ({
              value: descriptor,
              label: `${descriptor} · ${weight}`,
            }))
        : null,
    [faces],
  );

  const setFamily = (family: string) => {
    const picked = families.find((f) => f.family === family);
    let next = setPath(brand, "font.family", family);
    next = setPath(next, "font.faces", picked?.faces ?? null);
    // A slot holds a descriptor the family actually spells. Carrying
    // "ExtraBold" onto a family with no such face renders in a substitute and
    // reads as a bug in the engine.
    if (picked?.quad?.length === 4) {
      next = setPath(next, "font.slot_1", picked.quad[0]);
      next = setPath(next, "font.slot_2", picked.quad[1]);
      next = setPath(next, "font.slot_3", picked.quad[2]);
      next = setPath(next, "font.slot_4", picked.quad[3]);
    }
    onChange(next);
  };

  /* What each band currently WEARS, which is the value the four band pickers
     below show. Derived rather than stored: the slot ordinal is the model, the
     face is what a builder is choosing. */
  const bandFaces = useMemo(
    () => facesOfBands(brand.font, brand.weights),
    [brand.font, brand.weights],
  );

  const quad = `${brand.weights.titles}/${brand.weights.headings}/${brand.weights.leads}/${brand.weights.paragraphs}`;
  const quadOptions = WEIGHT_PRESETS.some((p) => p.value === quad)
    ? WEIGHT_PRESETS
    : [...WEIGHT_PRESETS, { value: quad, label: `${quad.replace(/\//g, " / ")} · this brand's` }];

  const family = families.find((f) => f.family === brand.font.family);

  return (
    <div className="ce-groups" data-rail-body>
      {/* ── 1–3 · the brand colour and its two adaptations ─────────────── */}
      <BrandColourSection
        brand={brand}
        colours={colours}
        flagsFor={flagsFor}
        onChange={onChange}
        onReveal={onReveal}
        onAction={onAction}
      />

      {/* ── 4 · Neutrals ───────────────────────────────────────────────── */}
      <Group name="Neutrals">
        <SwatchRow
          label="Neutrals"
          calm="The brand's own black and white. Good: 4.5 : 1 on every ground."
          entries={[
            { key: "black", name: "Black", value: brand.neutrals.black },
            { key: "white", name: "White", value: brand.neutrals.white },
          ]}
          flags={flagsUnder("neutrals")}
          colours={colours}
          onAction={onAction}
          onReveal={() => onReveal("neutrals.black")}
          onChange={(key, value) => set(`neutrals.${key}`, value)}
          expanded={neutralsOpen}
          onExpand={setNeutralsOpen}
        />
      </Group>

      {/* ── 5 · Signals ────────────────────────────────────────────────── */}
      <Group name="Signals">
        <SwatchRow
          label="Signals"
          calm="Four authored grounds. Good: each clears 4.5 : 1 for text."
          entries={SIGNAL_ROLES.map(([slot, role]) => ({
            key: slot,
            name: role,
            value: brand.signals[slot],
          }))}
          flags={flagsUnder("signals")}
          colours={colours}
          onAction={onAction}
          onReveal={() => onReveal("signals.error")}
          onChange={(key, value) => set(`signals.${key}`, value)}
          expanded={signalsOpen}
          onExpand={setSignalsOpen}
          /* THE INK OVERRIDE, on the role it is about (his ruling 2026-08-18).
             Only while `Edit each` is open: the collapsed row is four swatches in
             a 272px column and this is a second decision per role. */
          renderExtra={(key) => {
            const role = SIGNAL_ROLE_OF[key];
            const ink = role ? colours.signals?.[role] : undefined;
            return ink ? (
              <SignalInkChoice
                role={role as string}
                ink={ink}
                onChange={(value) => {
                  const next = { ...(brand.signals.foregrounds ?? {}) };
                  if (value === null) delete next[role as string];
                  else next[role as string] = value;
                  set("signals.foregrounds", Object.keys(next).length ? next : null);
                }}
              />
            ) : null;
          }}
        />
      </Group>

      {/* ── 6 · Typeface ───────────────────────────────────────────────── */}
      <Group name="Typeface">
        <SelectField
          label="Typeface"
          calm="Only families installed here. Good: four distinct weights."
          value={brand.font.family}
          /* THE HEADER SAYS WHAT THE TRIGGER DOES NOT. It used to print
             `JetBrains Mono · 8 faces` in the header AND the same string in the
             select 30px below — one value, twice, and the header wrapped to two
             lines doing it. */
          reads={family ? `${Object.keys(family.faces).length} faces` : null}
          options={
            families.length
              ? families.map((f) => ({
                  value: f.family,
                  label: `${f.family} · ${Object.keys(f.faces).length} faces`,
                }))
              : [{ value: brand.font.family, label: brand.font.family }]
          }
          flags={flagsFor("font.family")}
          colours={colours}
          onAction={onAction}
          onChange={setFamily}
          onReveal={() => onReveal("font.family")}
        />
      </Group>

      {/* ── Advanced · the topics ───────────────────────────────────────
          `Advanced (8) · closed` on first paint. `Weights` and `Radius` moved IN
          here from the primary list (his item 9): the typeface is the decision a
          reader judges, four weight slots and a radius ladder are its detail, and
          rule 1 still holds — either topic forces itself and this header open the
          moment it holds a flagged slot, so nothing needed to JUDGE the
          configuration is behind a closed disclosure. No capability was removed;
          both are one click from where they were. */}
      <Advanced flagged={ADVANCED_TOPICS.some((p) => flagsUnder(...p.under).length > 0)}>
        <Collapse title="Additional Colours" flags={flagsUnder("brand.additional")}>
          <AdditionalColours
            entries={brand.brand.additional}
            resolved={additional}
            max={colours.max_additional}
            onChange={(next) => set("brand.additional", next)}
          />
        </Collapse>

        <Collapse title="Weights" flags={flagsUnder("font.slot_1", "font.slot_2", "font.slot_3", "font.slot_4")}>
        <SelectField
          label="Weights"
          calm="One slot per role. Good: never lighter as text grows."
          value={quad}
          /* The trigger already prints the quad, so the header carries the
             EXPANDER instead of a second copy of it — one row rather than two,
             which is 40px of a 616px rail at 1280. */
          reads={
            <button
              type="button"
              className="ce-quiet"
              aria-expanded={weightsOpen}
              onClick={() => setWeightsOpen((v) => !v)}
            >
              {weightsOpen ? "Hide the four slots" : "Set the four slots"}
            </button>
          }
          options={quadOptions}
          flags={flagsUnder("font")}
          colours={colours}
          onAction={onAction}
          onChange={(value) => {
            const [t = 1, h = 1, l = 2, p = 3] = value.split("/").map(Number);
            let next = setPath(brand, "weights.titles", t);
            next = setPath(next, "weights.headings", h);
            next = setPath(next, "weights.leads", l);
            next = setPath(next, "weights.paragraphs", p);
            onChange(next);
          }}
          onReveal={() => onReveal("font.slot_1")}
        />
        {weightsOpen &&
          ([1, 2, 3, 4] as const).map((slot) =>
            faceOptions ? (
              <SelectField
                key={slot}
                label={`Weight slot ${slot}`}
                value={(brand.font as any)[`slot_${slot}`]}
                options={faceOptions}
                reads={(brand.font as any)[`slot_${slot}`]}
                flags={flagsFor(`font.slot_${slot}`)}
                colours={colours}
                onAction={onAction}
                onChange={(v) => set(`font.slot_${slot}`, v)}
                onReveal={() => onReveal(`font.slot_${slot}`)}
              />
            ) : (
              <ColourField
                key={slot}
                label={`Weight slot ${slot}`}
                value={(brand.font as any)[`slot_${slot}`]}
                calm="This family has no measured faces, so the slot takes a name."
                onChange={(v) => set(`font.slot_${slot}`, v)}
              />
            ),
          )}
        {weightsOpen &&
          (["titles", "headings", "leads", "paragraphs"] as const).map((role) => (
            <Fragment key={role}>
              {/* THE BAND PICKS A FACE, not a slot ordinal. This read
                  "Titles use -> slot 2", which asked a builder to know the
                  indirection exists and to make TWO writes for one decision:
                  set a slot's descriptor, then point the band at that slot.
                  The owner states it as one -- "for this font, Title is
                  ExtraBlack" -- so it is one control now. `assignFaceToBand`
                  does both writes and re-sorts the slots so the ladder stays
                  descending; see weights.ts for why the ordinal survives
                  underneath rather than being refactored away. When the family
                  has no measured faces the picker has nothing honest to offer,
                  so the slot ordinal stays visible instead. */}
              {faceOptions ? (
                <SelectField
                  label={`${role.charAt(0).toUpperCase()}${role.slice(1)}`}
                  value={bandFaces[role]}
                  reads={`slot ${brand.weights[role]}`}
                  options={faceOptions}
                  flags={flagsUnder("font")}
                  colours={colours}
                  onAction={onAction}
                  onChange={(descriptor) => {
                    const next = assignFaceToBand(
                      brand.font,
                      brand.weights,
                      role,
                      descriptor,
                      faces,
                    );
                    onChange({ ...brand, font: next.font, weights: next.weights });
                  }}
                />
              ) : (
                <SelectField
                  label={`${role.charAt(0).toUpperCase()}${role.slice(1)} use`}
                  value={String(brand.weights[role])}
                  reads={`slot ${brand.weights[role]}`}
                  options={SLOT_OPTIONS}
                  colours={colours}
                  onAction={onAction}
                  onChange={(v) => set(`weights.${role}`, Number(v))}
                />
              )}
              {/* THE CASE TOGGLE SITS WITH ITS BAND, not in a panel of its own.
                  Weight and case are the two things a brand authors ABOUT A BAND
                  (his ruling of 2026-08-19), the sheet publishes both on the same
                  `.ds-{band}` class, and a separate "Case" section would make a
                  reader hold the band name in two places to answer one question.
                  Segmented rather than a switch: two named states read faster
                  than an on/off whose off-state has no name. */}
              <SegmentedField
                label={`${role.charAt(0).toUpperCase()}${role.slice(1)} case`}
                value={brand.case[role] ? "uppercase" : "none"}
                options={CASE_OPTIONS}
                colours={colours}
                onAction={onAction}
                onChange={(v) => set(`case.${role}`, v === "uppercase")}
              />
            </Fragment>
          ))}
        </Collapse>

        {/* ── Radius ─────────────────────────────────────────────────────
            "base + S/M/L on ONE row" is the anatomy's own row 8, and it keeps
            that shape here — it simply moved behind `Advanced` with `Weights`
            (his item 9). A flagged `radius.base` still forces this open. */}
        <Collapse title="Radius" flags={flagsUnder("radius")}>
        <NumberField
          label="Radius"
          calm="S, M and L multiply this base. Good: 4–12 px; 0 is refused."
          value={brand.radius.base}
          unit="px"
          min={1}
          invalidMessage="At least 1 px — S, M and L multiply it."
          flags={flagsFor("radius.base")}
          colours={colours}
          onAction={onAction}
          onChange={(v) => set("radius.base", v)}
          onReveal={() => onReveal("radius")}
          beside={(["s", "m", "l"] as const).map((r) => (
            <div key={r} style={{ flex: 1, minWidth: 0 }}>
              <PxField
                bare
                inline
                label={r.toUpperCase()}
                base={brand.radius.base}
                factor={brand.radius[`${r}_factor`]}
                onChange={(f) => set(`radius.${r}_factor`, f)}
              />
            </div>
          ))}
        />
        </Collapse>

        <Collapse title="Border Weights" flags={flagsUnder("border")}>
          {[0, 1, 2, 3].map((i) => (
            <PxField
              key={i}
              bare
              label={`Weight ${i + 1}`}
              base={BASE}
              factor={brand.border.weight_factors[i] ?? 0}
              onChange={(f) => set(`border.weight_factors.${i}`, f)}
            />
          ))}
          <NumberField
            label="Border opacity"
            calm="0 invisible, 1 solid."
            value={brand.border.opacity}
            step={0.05}
            min={0}
            max={1}
            colours={colours}
            onAction={onAction}
            onChange={(v) => set("border.opacity", v)}
          />
        </Collapse>

        {/* ── THE MOTHER TABLE IS LOCKED (his ruling, 2026-08-18) ─────────
            `Type Sizes` (a rung selector plus 21 PxFields) and `Component Sizes`
            (an anchor, a step and seven role ratios) USED TO BE HERE, and they
            are deliberately gone: the type and component tables are engine-defined
            and a brand does not edit them. This REVERSES P4c's "the rail shows
            pixels and saves factors" — the editors it added were the capability
            he has now withdrawn, so removing them is the point rather than a
            regression.

            The one sizing choice a brand keeps is below: WHICH ROW of the engine's
            table it opens on. A pointer into the table, never a value in it, which
            is what keeps every brand's sizes comparable (rule 10). */}
        <Collapse title="Default Rung" flags={[]}>
          <SelectField
            label="Desktop"
            calm="The row of the engine's table this brand opens on. Good: L."
            value={brand.defaults.rung.desktop}
            reads={`${brand.defaults.rung.desktop} · type and components`}
            options={RUNG_OPTIONS}
            colours={colours}
            onAction={onAction}
            onChange={(v) => set("defaults.rung.desktop", v)}
            onReveal={() => onReveal("sizing")}
          />
          <SelectField
            label="Mobile"
            calm="Applies under 768px. Good: one or two rungs below desktop."
            value={brand.defaults.rung.mobile}
            reads={`${brand.defaults.rung.mobile} · under 768px`}
            options={RUNG_OPTIONS}
            colours={colours}
            onAction={onAction}
            onChange={(v) => set("defaults.rung.mobile", v)}
          />
        </Collapse>

        <Collapse title="Effects" flags={[]}>
          {Object.entries(brand.effects).map(([name, intensity]) => (
            <div key={name} className="ce-stack-tight">
              <span className="ce-label">{name}</span>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                  gap: 16,
                }}
              >
                {Object.entries(intensity as Record<string, number>).map(
                  ([field, factor]) => (
                    <PxField
                      key={field}
                      bare
                      label={field.replace(/_/g, " ")}
                      base={BASE}
                      factor={factor}
                      onChange={(f) => set(`effects.${name}.${field}`, f)}
                    />
                  ),
                )}
              </div>
            </div>
          ))}
        </Collapse>

        <Collapse title="Motion" flags={[]}>
          {Object.entries(brand.motion.speeds).map(([name, ms]) => (
            <NumberField
              key={name}
              label={name}
              value={ms as number}
              step={50}
              min={0}
              unit="ms"
              colours={colours}
              onAction={onAction}
              onChange={(v) => set(`motion.speeds.${name}`, v)}
            />
          ))}
          <SelectField
            label="Default speed"
            value={brand.motion.default_speed}
            reads={`${brand.motion.speeds[brand.motion.default_speed]} ms`}
            options={Object.keys(brand.motion.speeds).map((k) => ({ value: k, label: k }))}
            colours={colours}
            onAction={onAction}
            onChange={(v) => set("motion.default_speed", v)}
          />
          <SelectField
            label="Default curve"
            calm="A curve is a brand signature."
            value={brand.motion.default_curve}
            reads={brand.motion.default_curve}
            options={[
              ...Object.keys(brand.motion.curves).map((k) => ({ value: k, label: k })),
              ...(brand.motion.signature
                ? [{ value: "signature", label: "signature (this brand's own)" }]
                : []),
            ]}
            colours={colours}
            onAction={onAction}
            onChange={(v) => set("motion.default_curve", v)}
          />
        </Collapse>

        <Collapse title="Shadow Anchors" flags={flagsUnder("shadow")}>
          <NumberField
            label="Solid on white"
            calm="The shadow table hangs off these two."
            value={brand.shadow_anchors.solid}
            step={0.05}
            min={0}
            max={1}
            colours={colours}
            onAction={onAction}
            onChange={(v) => set("shadow_anchors.solid", v)}
            onReveal={() => onReveal("shadow")}
          />
          <NumberField
            label="Blurred on white"
            value={brand.shadow_anchors.blurred}
            step={0.05}
            min={0}
            max={1}
            colours={colours}
            onAction={onAction}
            onChange={(v) => set("shadow_anchors.blurred", v)}
            onReveal={() => onReveal("blur_and_shadow")}
          />
        </Collapse>
      </Advanced>
    </div>
  );
}

/**
 * The seven topics, and which flag prefixes force them open.
 *
 * Declared once so the `Advanced` header can ask "is anything in here flagged"
 * without rendering the topics to find out — which is the whole point of it
 * being closed.
 */
const ADVANCED_TOPICS: { title: string; under: string[] }[] = [
  { title: "Additional Colours", under: ["brand.additional"] },
  { title: "Border Weights", under: ["border"] },
  /* `Type Sizes` and `Component Sizes` are GONE — the mother table is locked
     (his ruling, 2026-08-18). What a brand still chooses is which row of it to
     open on, which is `Default Rung`. */
  { title: "Default Rung", under: ["sizing"] },
  { title: "Weights", under: ["font.slot_1", "font.slot_2", "font.slot_3", "font.slot_4"] },
  { title: "Radius", under: ["radius"] },
  { title: "Effects", under: [] },
  { title: "Motion", under: [] },
  { title: "Shadow Anchors", under: ["shadow"] },
];

/**
 * `Advanced (7)`, closed — one row until it is asked for.
 *
 * It opens itself when a topic inside holds a flagged slot, for the same reason
 * a topic does: nothing needed to judge the configuration may sit behind a
 * closed disclosure.
 */
function Advanced({
  flagged,
  children,
}: {
  flagged: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(flagged);
  return (
    <section data-group="Advanced" data-open={open || undefined} className="ce-stack-tight">
      <button
        type="button"
        className="ce-row"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        style={{
          width: "100%",
          minHeight: 32,
          gap: 8,
          background: "transparent",
          border: 0,
          padding: 0,
          color: "inherit",
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        <span aria-hidden className="ce-body ce-2" style={{ width: 12 }}>
          {open ? "▾" : "▸"}
        </span>
        <span className="ce-h2">Advanced</span>
        <span className="ce-body ce-2" style={{ marginLeft: "auto" }}>
          {ADVANCED_TOPICS.length} topics
        </span>
      </button>
      {open && (
        <div className="ce-stack-tight" style={{ marginTop: 8 }}>
          {children}
        </div>
      )}
    </section>
  );
}

/* ------------------------------------------------------------- additional */

/**
 * The additional colours: add, remove, recolour, label.
 *
 * The swatch renders the PAIR — the colour with the engine's own ink on it —
 * because the pair is what a consumer receives. Beside it sits the EMITTED
 * name, permanently, so nothing implies that renaming the label repoints a
 * `var()`: the emitted names are positional and a label is for the eye.
 */
function AdditionalColours({
  entries,
  resolved,
  max,
  onChange,
}: {
  entries: { value: string; label: string }[];
  resolved: AdditionalColour[];
  max: number;
  onChange: (next: { value: string; label: string }[]) => void;
}) {
  const patch = (index: number, part: Partial<{ value: string; label: string }>) =>
    onChange(entries.map((entry, i) => (i === index ? { ...entry, ...part } : entry)));

  return (
    <div className="ce-stack" data-section="additional">
      {entries.length === 0 && (
        <p className="ce-body ce-2">
          None yet. Add one and it joins the emitted vocabulary as
          {" --additional-1 "}
          with its own computed foreground — free for slides, charts, anything.
        </p>
      )}

      {entries.map((entry, index) => {
        const answer = resolved[index];
        const emitted = answer?.name ?? `additional-${index + 1}`;
        return (
          <div key={index} data-additional={index + 1} className="ce-stack-tight">
            <div className="ce-row">
              <span
                aria-hidden
                data-swatch={emitted}
                className="ce-swatch"
                style={{
                  background: entry.value,
                  color: answer?.foreground ?? undefined,
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 10,
                }}
              >
                Aa
              </span>
              <input
                type="color"
                aria-label={`additional ${index + 1} colour`}
                value={entry.value}
                onChange={(e) => patch(index, { value: e.target.value })}
                className="ce-control"
                style={{ width: 40, padding: 0, cursor: "pointer" }}
              />
              <input
                aria-label={`additional ${index + 1} label`}
                placeholder={`additional-${index + 1}`}
                value={entry.label}
                onChange={(e) => patch(index, { label: e.target.value })}
                className="ce-control"
                style={{ flex: 1, minWidth: 0, fontFamily: "var(--font-mono)" }}
              />
              <button
                type="button"
                className="ce-quiet"
                aria-label={`remove additional ${index + 1}`}
                onClick={() => onChange(entries.filter((_, i) => i !== index))}
              >
                ×
              </button>
            </div>
            <span className="ce-micro ce-2" style={{ fontFamily: "var(--font-mono)" }}>
              {entry.value} · emitted as --{emitted}
              {answer ? ` · ink ${answer.foreground}` : ""}
            </span>
          </div>
        );
      })}

      <div className="ce-row">
        <button
          type="button"
          className="ce-chip"
          data-testid="add-additional-colour"
          disabled={entries.length >= max}
          onClick={() => onChange([...entries, { value: "#888888", label: "" }])}
        >
          Add a colour
        </button>
        <span className="ce-micro ce-2">
          {entries.length} of {max}
        </span>
      </div>
    </div>
  );
}
