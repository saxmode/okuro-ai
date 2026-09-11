/**
 * The resolved model, as the engine serves it.
 *
 * These types are a mirror of `okuro/design_engine/api.py`'s serialisers and
 * nothing more. There is deliberately no computation on this side of the wire:
 * every colour, size and rule below was decided in Python, and a TypeScript
 * reimplementation of any of it would be a second opinion about a decision the
 * engine has already made.
 */

export type Polarity = "dark" | "light";

/**
 * A flag, with BOTH of its addresses.
 *
 * He ruled that advice belongs in two places at once — "both configuration
 * (something is not good with this value) and visibility (here is where it's
 * failing)". `where` is the authored slot in the rail; `renders` names the
 * palette slots the value is actually painted on, so the canvas can mark the
 * exact component. The engine fills `renders` because only the engine knows
 * which rule puts a value where.
 */
/**
 * How bad a finding is. The ENGINE ranks; this page never does.
 *
 * A page that assigned severity would be a second opinion about the engine's
 * own findings — it would have to know that WCAG AA on text is a legal gate
 * while 3:1 on a figure is a lint reference, and that a shadow becoming a
 * border is the system working. Those facts live in `design_engine/colour.py`.
 */
export type Severity = "must-fix" | "decide" | "note";

export interface Flag {
  code: string;
  message: string;
  where: string;
  renders: string[];
  /** From `colour.SEVERITY`. Optional only so an old cached payload still
      parses; `severity.ts` falls back to the same table, keyed by code. */
  severity?: Severity;
}

export interface AlphaStep {
  ink: string;
  percent: number;
  over: string | null;
  css: string;
}

export interface Ladder {
  ink: string;
  steps: AlphaStep[];
}

export interface ShadowAnswer {
  kind: "shadow" | "border";
  colour: string;
  opacity: number;
  width: number | null;
  reason: string;
}

export interface StateLayer {
  name: string;
  colour: string;
  ink: string;
  border_width: number;
  outline?: { colour: string; width: number; offset: number };
}

export interface ComponentSurface {
  style: string;
  fill: string;
  ink: string;
  border: string;
  border_branded: string;
  states: Record<"normal" | "hover" | "pressed" | "focus", StateLayer>;
}

export interface Palette {
  background: string | null;
  foreground: string;
  highlight_neutral_background: string;
  highlight_neutral_foreground: string;
  highlight_branded_background: string;
  highlight_branded_foreground: string;
  alternate: Ladder;
  alternate_inverse: Ladder;
  separator: AlphaStep;
  border_full: string;
  border_half: AlphaStep;
  border_branded: string;
  border_branded_inverse: string;
  focus: AlphaStep;
  solid: string;
  solid_inverse: string | null;
  solid_brand: string;
  solid_brand_inverse: string;
  transparent: AlphaStep;
  transparent_inverse: AlphaStep;
  blur_background: AlphaStep;
  shadow: ShadowAnswer;
  blur_and_shadow: ShadowAnswer;
  signal_backgrounds: Record<string, string>;
  signal_foregrounds: Record<string, string>;
  states: {
    hover: AlphaStep;
    hover_brand: AlphaStep;
    pressed: string;
    pressed_brand: string;
    focus: AlphaStep;
    border_widths: Record<string, number>;
  };
  disabled: number;
  off: number;
  flags: Flag[];
}

/** Where one derived value came from. The engine's own sentence, not ours. */
export interface Derivation {
  slot: string;
  label: string;
  value: string;
  rule: string;
  reads: string[];
  because: string;
  swatch: string | null;
  stage: string | null;
}

export interface GraphNode {
  id: string;
  /**
   * THREE KINDS. `overridden` is v1's fourth origin kind, ported because v2
   * could express the mechanism but not the fact: a slot whose value a curator
   * authored over the rule's answer arrived here as `derived`, which asserts
   * the rule produced it.
   */
  kind: "authored" | "derived" | "overridden";
  /** Roles a curator authored over, present only when kind is "overridden". */
  overridden?: string[];
  label: string;
  value: string;
  swatch: string | null;
  group: string;
  rule: string;
  because?: string;
}

export interface ResolvedGround {
  key: string;
  colour: string | null;
  lightness: number;
  polarity: Polarity;
  origin: string;
  in_warn_band: boolean;
  palette: Palette;
  /** Every builder action -> the ground key it lands on. The whole descent. */
  transitions: Record<string, string>;
  components: ComponentSurface[];
  derivations: Derivation[];
  graph: { nodes: GraphNode[]; edges: [string, string][] };
}

export interface RootChoice {
  id: string;
  label: string;
  /** Only dark and light are true. Brand and signals are builder placements. */
  user_choice: boolean;
  key: string;
}

export interface AuthoredInput {
  slot: string;
  label: string;
  value: string;
  group: string;
  swatch: string | null;
  /** The engine's own OKLab L. Colour slots only; never measured on this side. */
  lightness: number | null;
}

/**
 * One authored size slot, both ways.
 *
 * `factor` is what a brand SAVES; `px` is what a designer READS. A UI that
 * persisted the pixel would be left behind the moment the base changed — the
 * frozen-grid failure ruling 4 killed — so the two always travel together and
 * the editing surface never receives a pixel on its own.
 */
export interface SizeSlot {
  factor: number;
  px: number;
}

/**
 * The authored factors.
 *
 * TEXT is HIS table — 8 rungs x 21 styles of factor, authored per cell on
 * 2026-08-17 — so it travels whole and has no anchor or ratio ladder behind it.
 * COMPONENTS are still the engine's anchor, step and seven role ratios, because
 * he authored no component table.
 */
export interface SizeFactors {
  text: Record<string, Record<string, number>>;
  component_anchor: number;
  component_rung_step: number;
  role_ratios: Record<string, number>;
}

/** T = title, H = heading, N = normal. His letters, one vocabulary. */
export interface TextBand {
  letter: string;
  name: string;
  count: number;
}

export interface Sizes {
  /* The three system CONSTANTS, reported so the page can state the rule.
     Nothing here is editable: root 50 %, root_px 8, base 8 — and because
     root_px IS base, an emitted rem is the factor. */
  root_percent: number;
  root_px: number;
  base: number;
  factors: SizeFactors;
  radius_factors: Record<"S" | "M" | "L", SizeSlot>;
  text_rungs: string[];
  component_rungs: string[];
  text_styles: string[];
  text_bands: TextBand[];
  component_roles: string[];
  text: Record<string, Record<string, number>>;
  components: Record<string, Record<string, number>>;
  downscale: { text: Record<string, string>; components: Record<string, string> };
  radius: Record<string, number>;
  radius_base: number;
}

export interface TypeModel {
  family: string;
  stack: string;
  slots: { slot: number; descriptor: string; numeric: number | null }[];
  offered: { descriptor: string; numeric: number }[];
  assignment: Record<"titles" | "headings" | "leads" | "paragraphs", number>;
}

export interface ResolvedModel {
  brand_id: string;
  brand: BrandJson;
  authored: AuthoredInput[];
  threshold: number;
  warn_band: [number, number];
  roots: RootChoice[];
  grounds: ResolvedGround[];
  requests: string[];
  sizes: Sizes;
  type: TypeModel;
  motion: {
    speeds: Record<string, number>;
    curves: Record<string, string>;
    signature: string | null;
    default_curve: string;
    default_speed: string;
  };
  effects: Record<string, Record<string, number>>;
  border: { weights: number[]; opacity: number };
  brand_colours: BrandColourModel;
  additional: AdditionalColour[];
  flags: Flag[];
}

/**
 * One additional brand colour, with the foreground the ENGINE computed for it.
 *
 * The foreground is here rather than in the authored brand because it is
 * derived — the same threshold rule as everything else. A page that computed it
 * would be a second implementation of the one measurement the system takes.
 */
export interface AdditionalColour {
  /** The EMITTED name, positional: `additional-1` … `additional-10`. */
  name: string;
  /** The builder's own label. Renaming it must not repoint a var(). */
  label: string;
  value: string;
  polarity: "light" | "dark";
  foreground: string;
}

/** One pole of the brand colour: the shade behind it, and what the engine advises. */
export interface BrandPole {
  /** The resolved colour a branded figure on this ground actually takes. */
  value: string;
  /** The authored shade AMOUNT, or null for a pole authored as a colour. */
  shade: number | null;
  authored_colour: string | null;
  /** `growth.propose_adaptation` — the least amount that clears the threshold. */
  proposed_shade: number;
  proposed_value: string;
  reason: string;
  /** The brand's own black or white the shade walks toward. */
  toward: string;
}

/**
 * The brand-colour model, as the page renders it.
 *
 * ONE BRAND IS ONE COLOUR by default: `canonical` is authored and each pole is a
 * SHADE of it. `two_colour_mode` is the explicit toggle into two independently
 * authored colours, and only then are the free pickers offered.
 */
/**
 * One signal's ink, and both candidates for it — his ruling of 2026-08-18.
 *
 * The page never computes a contrast ratio: all three numbers come from the
 * engine's own `wcag_contrast`, so what the rail prints and what the flag says
 * are one measurement.
 */
export interface SignalInk {
  background: string;
  /** What rule 2 computes: the brand's own black or white, by polarity at the
   * one threshold. */
  computed: string;
  /** What the brand authored instead, or null when it accepted the computation. */
  authored: string | null;
  /** Whichever of the two the engine actually resolved. */
  resolved: string;
  computed_contrast: number;
  /** The other neutral — the only other ink rule 13 allows. */
  alternative: string;
  alternative_contrast: number;
  authored_contrast: number | null;
  /** WCAG AA for text, from the engine. Never hard-coded on the page. */
  aa: number;
}

export interface BrandColourModel {
  canonical: string;
  two_colour_mode: boolean;
  max_additional: number;
  poles: { light: BrandPole; dark: BrandPole };
  /** Per signal role: error / success / info / warning. */
  signals: Record<string, SignalInk>;
}

/** The AUTHORED brand. The only thing the page ever edits. */
export interface BrandJson {
  id: string;
  signals: {
    red: string;
    green: string;
    blue: string;
    pink: string;
    /**
     * AUTHORED foreground overrides, keyed by ROLE — his ruling of 2026-08-18.
     *
     * Absent or null means "compute it", which is what every brand said before
     * this slot existed. A role present here wins over rule 2's computation and
     * is FLAGGED, never rejected.
     */
    foregrounds?: Record<string, string> | null;
  };
  neutrals: { black: string; white: string };
  /** The brand's opening rung, per viewport class. The mother table is locked;
   *  this is the pointer into it that a brand still owns. */
  defaults: { rung: { desktop: string; mobile: string } };
  brand: {
    canonical: string;
    on_light: string | null;
    on_dark: string | null;
    /** The explicit toggle. False (the default) means one brand, one colour. */
    two_colour_mode: boolean;
    /** Fractions of the brand's own black / white, 0..1. Null = untouched. */
    shade_light: number | null;
    shade_dark: number | null;
    /** 0..10 free palette entries. The engine computes each one's foreground. */
    additional: { value: string; label: string }[];
  };
  font: {
    family: string;
    stack: string;
    slot_1: string;
    slot_2: string;
    slot_3: string;
    slot_4: string;
    faces: Record<string, number> | null;
  };
  weights: { titles: number; headings: number; leads: number; paragraphs: number };
  /**
   * WHICH BANDS ARE SET IN UPPERCASE — his ruling of 2026-08-19, one boolean per
   * band. Same four keys as `weights` on purpose: they are the same four bands,
   * and the sheet publishes both facts on the same `.ds-{band}` class.
   */
  case: { titles: boolean; headings: boolean; leads: boolean; paragraphs: boolean };
  /** FACTORS of the system base, not pixels. On base 8: 1/2/4/8 px. */
  border: { weight_factors: [number, number, number, number]; opacity: number };
  /** S/M/L are factors of the RADIUS base, which is its own authored number. */
  radius: { base: number; s_factor: number; m_factor: number; l_factor: number };
  /** Every field is a FACTOR of the system base. */
  effects: Record<string, Record<string, number>>;
  motion: {
    speeds: Record<string, number>;
    curves: Record<string, string>;
    signature: string | null;
    default_curve: string;
    default_speed: string;
  };
  shadow_anchors: { solid: number; blurred: number };
  /**
   * The authored factor tables.
   *
   * `text_factors` is his own 168 cells, keyed rung -> style -> factor. A
   * component cell is still `rung_factor x role_ratio`, because he authored no
   * component table. Both are factors of `base`, never pixels.
   */
  sizes: {
    text_factors: Record<string, Record<string, number>>;
    component_anchor_factor: number;
    component_rung_step: number;
    role_ratios: Record<string, number>;
    component_rung_factor: Record<string, number> | null;
  };
  /*
   * NO `base` AND NO `root_percent`. Both left the authored brand with his
   * ruling of 2026-08-17 — "Brands do not change their base. Base is always
   * the same." They are `BASE` and `ROOT_PERCENT` in ./scale, and the engine
   * still reports them under `sizes` so the tables can state the rule.
   */
}

export interface Stage {
  key: string;
  title: string;
  caption: string;
  reveals: string[];
}

export interface Rule {
  id: string;
  title: string;
  formula: string;
  source: string;
}

export interface FontFamily {
  family: string;
  faces: Record<string, number>;
  quad: string[];
}

export interface Adaptation {
  pole: string;
  value: string;
  amount: number;
  toward: string;
  reason: string;
}

export interface EssentialsResult {
  brand: BrandJson;
  on_light: Adaptation;
  on_dark: Adaptation;
  weight_note: string;
  flags: Flag[];
}

/**
 * One guest brand, resolved on one host ground.
 *
 * `outcome` is where it landed; `clause` names WHICH half of register rule 4
 * decided it; `because` is the measurement. Two guests on the same host ground
 * can differ, and nothing configures that — it falls out of the one rule read
 * against each guest's own colours.
 */
export interface Placement {
  guest: string;
  outcome: "brand-full" | "brand-fallback";
  clause: string;
  ground: string | null;
  polarity: Polarity;
  cta: string;
  cta_slot: string;
  because: string;
  host_ground: string | null;
  host_polarity: Polarity;
  host_is_neutral_to_guest: boolean;
}

export interface NestingResult {
  css: string;
  host: {
    id: string;
    key: string;
    colour: string | null;
    lightness: number;
    polarity: Polarity;
    origin: string;
  };
  placements: Placement[];
  rule: Rule;
  flags: Flag[];
}

export interface KitRow {
  id: string;
  origin: "package" | "user";
  editable: boolean;
  /** The canonical brand colour — the swatch a row is recognised by. Absent
      when the kit would not load. */
  colour?: string;
  /** The typeface family. Absent for the same reason. */
  font?: string;
  /** The file is there and will not parse. The row is listed anyway: an
      overview that hides a broken kit is how one stays broken. */
  unreadable?: boolean;
}

/** A node of the tree the BUILDER composes. Ground keys come from the engine. */
export interface TreeNode {
  id: string;
  /** null means "no request" — which is inheritance, expressed as absence. */
  request: string | null;
  groundKey: string;
  children: TreeNode[];
}
