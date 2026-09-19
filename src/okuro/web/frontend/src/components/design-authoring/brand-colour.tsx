/**
 * THE BRAND COLOUR — decisions one, two and three, page-neutral.
 *
 *     "ONE BRAND = ONE COLOUR, DEFAULT: canonical is chosen, and on-light/
 *      on-dark are SHADES OF THAT COLOUR (adjustment flow), not free colours.
 *      A brand genuinely using two different colours must TOGGLE into that mode
 *      explicitly."
 *
 * So one colour picker, then two shade controls. The engine's proposal is drawn
 * on the same track the shade moves along, and it is a LABELLED CONTROL rather
 * than a tooltip: a visible tick, the number printed beside it, and a button
 * that takes it. Measured before, the proposal was a 1 × 16px line whose only
 * affordance was a hover, sitting next to a slider thumb that scored 1.00 : 1
 * against the page it was drawn on. The most useful number the engine computes
 * was the least visible thing on the rail.
 *
 * NOTHING HERE COMPUTES A COLOUR. The slider sends an AMOUNT and the engine
 * returns the colour, which is why the amount is what gets stored: a page that
 * shaded the hex itself would be a second implementation of the adjustment,
 * diverging exactly at the threshold where it matters.
 *
 * ── WAVE 2, 2026-09-13 · WHY IT MOVED AND WHAT THE SEAM IS ──────────────────
 *
 * It lived in `components/design-engine/` and only `/design-engine` could reach
 * it, so DESIGN — `/ds-engine-codex` — answered the same questions again with
 * its own `ColorField` and `ShadeField`. Two rails, one kit store, two records
 * for "this brand now uses that colour": the old rail dropped the literal
 * adaptations when the canonical moved and rebuilt a coherent record on the mode
 * toggle; the codex panel wrote the one field and left the rest standing.
 *
 * THREE THINGS THE HOST SUPPLIES, and they are exactly the three that are chrome
 * rather than logic (convention 786f04cd — logic is shared, chrome is not):
 *
 *   `vocabulary`    the class names this block paints with.
 *   `renderColour`  the host's own colour ROW — label, swatch, hex, aria. A
 *                   field shell is chrome: the old page's carries an advice
 *                   slot, DESIGN's carries `aria-describedby` into its own
 *                   findings list, and neither may be imposed on the other.
 *   `renderHelp`    the host's own findings slot under a pole.
 *
 * WHAT IS NOT NEGOTIABLE, because it is the logic this file exists to hold once:
 * the two-colour record in both directions, the canonical edit that drops the
 * literal adaptations, the shade writes, and the proposal control.
 *
 * THE THRESHOLD IS READ, NEVER TYPED. It used to come from `severity.ts`'s
 * `POLARITY_THRESHOLD`, a frontend mirror of `colour.POLARITY_THRESHOLD`. The
 * engine publishes it on the resolved model, so the host hands it in — "a
 * projection that computes is a second authority", and a mirror is a projection
 * that computed once and stopped listening.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Slider } from "radix-ui";
import { Switch } from "@/components/ui/switch";
import type { AuthoringVocabulary } from "./vocabulary";
import type {
  BrandColourModel,
  BrandJson,
  BrandPole,
  Flag,
} from "@/components/design-engine/types";

/* ------------------------------------------------------------------- seam */

/**
 * One colour decision, as the host is asked to draw it.
 *
 * `slot` is the AUTHORED PATH the control writes and the address a flag's
 * `where` names — one string for both, so a host cannot place a finding under a
 * control that does not write that value.
 */
export interface AuthoredColour {
  label: string;
  slot: string;
  value: string;
  /** What this setting does, in one sentence. */
  calm: string;
  flags: Flag[];
  onChange: (next: string) => void;
  onReveal?: () => void;
  /**
   * Part of the SAME decision — the two-colour toggle. The host decides WHERE
   * it sits: `/design-engine` puts it on the control's own row, DESIGN gives it
   * a row of its own. Neither placement is imposed, which is what keeps each
   * page's layout its own.
   */
  beside?: ReactNode;
  /** A sentence for hosts whose layout has room for one. */
  besideHint?: string;
}

/** The findings slot under one pole, as the host is asked to draw it. */
export interface AuthoredHelp {
  slot: string;
  calm: string;
  flags: Flag[];
}

export interface BrandColourSectionProps {
  brand: BrandJson;
  colours: BrandColourModel;
  /** `model.threshold` — the engine's own constant, never a mirror of it. */
  threshold: number;
  vocabulary: AuthoringVocabulary;
  flagsFor: (slot: string) => Flag[];
  onChange: (next: BrandJson) => void;
  onReveal?: (slot: string) => void;
  renderColour: (field: AuthoredColour) => ReactNode;
  renderHelp?: (help: AuthoredHelp) => ReactNode;
  /**
   * Does the host's own findings slot already offer to set this shade?
   *
   * `[Use 32 %]` here and `[Set 32 %]` in a note 130px below are two buttons for
   * one edit, and the reader's question becomes "which do I press". The host
   * knows, because the host renders the note; this block only asks. A host with
   * no actionable advice says no, and the row keeps the control — which is the
   * correct answer for a surface that has no advice machinery at all.
   */
  helpOffersShade?: (flags: Flag[]) => boolean;
}

/* ------------------------------------------------------------------ slider */

/**
 * A shade slider whose thumb can actually be seen.
 *
 * THE HANDLE IS LOCAL WHILE IT MOVES, and that is a correctness fix rather than
 * a smoothness one. The authoritative amount arrives back through a 140ms
 * debounced re-resolve, so a slider controlled directly by it snaps back to the
 * last ANSWERED value between keystrokes — measured, 24 presses of ArrowRight
 * landed on 1 %, because 23 of them were reverted before the round trip
 * returned. The handle owns its position while it is being moved and accepts
 * the model's value again only when the model disagrees with what this
 * component last sent, which is how an edit made ELSEWHERE still moves it.
 */
function ShadeSlider({
  label,
  amount,
  proposed,
  v,
  onChange,
}: {
  label: string;
  amount: number;
  proposed: number;
  v: AuthoringVocabulary;
  onChange: (next: number) => void;
}) {
  const [local, setLocal] = useState(amount);
  const sent = useRef(amount);

  useEffect(() => {
    if (Math.abs(amount - sent.current) > 0.0005) {
      sent.current = amount;
      setLocal(amount);
    }
  }, [amount]);

  const move = (next: number) => {
    sent.current = next;
    setLocal(next);
    onChange(next);
  };

  return (
    <div className="relative flex items-center" style={{ height: 24 }}>
      <Slider.Root
        aria-label={label}
        value={[Math.round(local * 100)]}
        min={0}
        max={98}
        step={1}
        onValueChange={(values) => move((values[0] ?? 0) / 100)}
        className="relative flex w-full touch-none items-center select-none"
        style={{ height: 24 }}
      >
        <Slider.Track
          className="relative w-full grow"
          style={{
            height: 4,
            borderRadius: 2,
            background: v.border,
          }}
        >
          <Slider.Range
            className="absolute"
            style={{ height: "100%", borderRadius: 2, background: v.mutedForeground }}
          />
        </Slider.Track>
        {/* 16px, a 1px border and a shadow. Measured before: 14px, a
            TRANSPARENT border and `bg-background` — 1.00 : 1 against the page,
            which is a thumb you cannot see at all. */}
        {/* Geometry and ink live in each host's own stylesheet, keyed on
            `[role="slider"]`: 16px of paint inside a 24px target, so "every
            interactive element is at least 24 x 24" and "the thumb reads as a
            16px dot" are both true of the same element. An inline style here
            would win over that rule and quietly reinstate a 14px thumb — and it
            would also be this component picking one page's look. */}
        <Slider.Thumb className="block outline-none" />
      </Slider.Root>
      {/* THE PROPOSAL, ON THE TRACK IT IS A PROPOSAL ABOUT — as a tick, not as
          a tooltip. The number and the action live on the row below, at a size
          a person can hit. */}
      <span
        aria-hidden
        data-proposed={proposed}
        style={{
          position: "absolute",
          left: `${Math.min(proposed, 0.98) * 100}%`,
          top: 2,
          width: 2,
          height: 20,
          background: v.mutedForeground,
          pointerEvents: "none",
        }}
      />
    </div>
  );
}

/* -------------------------------------------------------------------- pole */

/** One pole: a shade of the one colour, its result, and the engine's proposal. */
function PoleShade({
  pole,
  model,
  flags,
  v,
  onShade,
  onReveal,
  renderHelp,
  helpOffersShade,
}: {
  pole: "light" | "dark";
  model: BrandPole;
  flags: Flag[];
  v: AuthoringVocabulary;
  onShade: (amount: number | null) => void;
  onReveal?: () => void;
  renderHelp?: (help: AuthoredHelp) => ReactNode;
  helpOffersShade?: (flags: Flag[]) => boolean;
}) {
  // A pole authored as a COLOUR before the shade model existed has no amount.
  // Seeding the slider with the engine's proposal would move a value the brand
  // never asked to move, so it starts at 0.
  const amount = model.shade ?? 0;
  const percent = Math.round(amount * 100);
  const proposed = Math.round(model.proposed_shade * 100);
  const label = pole === "light" ? "On light grounds" : "On dark grounds";
  const slot = `brand.on_${pole}`;

  const noteOffersShade = Boolean(helpOffersShade?.(flags));

  /**
   * THE ENGINE'S SENTENCE, THEN THE ONE HALF IT DOES NOT PUBLISH.
   *
   * `BrandPole.reason` is what `growth.propose_adaptation` says it did and why;
   * this block used to PARAPHRASE it ("Toward the brand's own black"), which is
   * a page restating a rule the engine owns. What the engine does not say is what
   * GOOD looks like, so that is the only half still written here — and it is
   * written SHORT on purpose.
   *
   * THE LENGTH IS A MEASUREMENT, not a preference. The help slot reserves two
   * lines; a third costs 20px per pole, and at 1440 the rail has 36px of headroom
   * against his 1,122px budget (`test_walkthrough::test_item_9`). A first draft
   * carried "Good: the 39 % that crosses 0.675" and the rail measured 1,158.
   * The threshold is not lost — it is on the two-colour calms, on every flag the
   * engine writes, and in the model both rails read.
   */
  const calm = `${model.reason} Good: ${proposed} %.`;

  const chips = [
    /* THE PROPOSAL AS A LABELLED CONTROL — and ONLY when no note is offering
       the same act. `[Use 32 %]` here and `[Set 32 %]` in the note 130px below
       were two buttons for one edit, in one group, and the reader's question was
       "which do I press". The note owns it when there is a note that performs
       it; the row owns it otherwise, and it carries the same `data-use-proposal`
       either way. */
    percent !== proposed && !noteOffersShade ? (
      <button
        key="use"
        type="button"
        className={v.chip}
        data-use-proposal={pole}
        onClick={() => onShade(model.proposed_shade)}
      >
        Use {proposed} %
      </button>
    ) : null,
  ].filter(Boolean);

  return (
    /* NOT ITS OWN GROUP — the two poles share one (his item 9). "On light
       grounds" and "On dark grounds" are the SAME question asked twice about one
       colour, so they are two controls of one decision rather than two
       decisions. Measured before: 181px each, and a 32px group gap plus a
       hairline between them for a boundary that was not a real one. */
    <div
      data-control={label}
      data-pole={pole}
      data-mode="one-colour"
      /* PUBLISHED AS AN ATTRIBUTE as well as shown in the help slot, so a gate
         can ask "is this the engine's sentence" without depending on which of
         the help slot's states the host happens to be rendering. */
      data-shade-reason={model.reason}
      className={v.stackTight}
    >
      <div className={v.row} style={{ gap: 8 }}>
        <button
          type="button"
          className={v.label}
          onClick={onReveal}
          style={{
            background: "transparent",
            border: 0,
            padding: 0,
            minHeight: 24,
            color: v.strongForeground,
            cursor: "pointer",
          }}
        >
          {label}
        </button>
        {/* A NUMBER, NOT THE WORD `authored`. The literal string was the
            readout for a pole with no amount, so the one place the rail
            reported a quantity reported a category instead.
            THE RESOLVED COLOUR RIDES WITH IT rather than on a row of its own:
            one decision, one readout, and 24px of a 616px rail back. */}
        <span className={v.row} style={{ marginLeft: "auto", gap: 4 }}>
          {/* THE READOUT IS THE AMOUNT AND NOTHING ELSE — `/^\d{1,3} %$/`.
              The colour it produces rides on the same ROW, in its own
              element, because one decision deserves one row; folding the hex
              into this span would make the one place the rail reports a
              quantity report two things. */}
          <span className={v.value} data-shade-reads>
            {percent} %
          </span>
          <span
            aria-hidden
            data-swatch={`on-${pole}`}
            className={v.swatch}
            style={{ background: model.value }}
          />
          <span className={v.value} style={{ fontFamily: "var(--font-mono)" }}>
            {model.value}
          </span>
          {/* BACK TO NO AMOUNT AT ALL — not the same act as taking the
              proposal. `null` means "this brand authored no shade here", so the
              engine re-proposes from whatever the canonical becomes; a literal
              amount pins 39 % onto a colour that may never have needed
              darkening. That is his colour rule in the one control that can
              violate it. DESIGN had this and the old rail did not; wave 2 gives
              it to both, because neither page may hold a capability the other
              cannot reach through the shared core.

              IT SITS ON THE READOUT ROW, and that placement is a measurement.
              On a row of its own it cost 24px plus a stack gap, and at 1440 the
              rail measured 1,138px against his 1,122px budget
              (`test_walkthrough::test_item_9`). His ruling about how tall the
              rail may be is not something a new control gets to spend. This row
              is already 24px tall and the button is 24 x 24, so it adds nothing.
              The glyph carries a full accessible name rather than a hover: what
              that audit punished was a 1 x 16px tick with no hit target, which
              this is not. */}
          {model.shade != null && (
            <button
              type="button"
              className={v.quiet}
              data-clear-shade={pole}
              aria-label={`Clear the ${label.toLowerCase()} shade and let the engine propose ${proposed} % again`}
              title={`Back to the engine's ${proposed} %`}
              onClick={() => onShade(null)}
            >
              ↺
            </button>
          )}
        </span>
      </div>

      <ShadeSlider
        label={`${label} shade`}
        amount={amount}
        proposed={model.proposed_shade}
        v={v}
        onChange={onShade}
      />

      {/* AN EMPTY ROW IS STILL A ROW, and a stack pays two gaps for it.
          Measured on `/design-engine` at 1280: with both chips withheld this
          wrapper rendered at height 0 and the rhythm between the reason and the
          help slot read 20px — 8 + 8 + 4 — where `chrome.css` spends 12 between
          a control and its help slot. `test_page_contract::test_c3` caught it,
          which is what that criterion is for. So the wrapper is rendered only
          when it has something in it. */}
      {chips.length > 0 && (
        <div className={v.row} style={{ gap: 8 }}>
          {chips}
        </div>
      )}

      {renderHelp?.({ slot, calm, flags })}
    </div>
  );
}

/* ----------------------------------------------------------------- section */

export function BrandColourSection({
  brand,
  colours,
  threshold,
  vocabulary: v,
  flagsFor,
  onChange,
  onReveal,
  renderColour,
  renderHelp,
  helpOffersShade,
}: BrandColourSectionProps) {
  const setBrandColours = (part: Partial<BrandJson["brand"]>) =>
    onChange({ ...brand, brand: { ...brand.brand, ...part } });

  /**
   * The toggle, both ways, and each direction has to leave a COHERENT record.
   *
   * Into two-colour mode: the shades are dropped and the colours they produced
   * are kept, so the pickers open on what was already on screen. Back to one
   * colour: the free colours are dropped, because keeping them would leave a
   * brand claiming one colour while storing three, and the shades come back at
   * the engine's proposal — the same place the default flow starts.
   */
  const setMode = (two: boolean) => {
    if (two) {
      setBrandColours({
        two_colour_mode: true,
        shade_light: null,
        shade_dark: null,
        on_light: colours.poles.light.value,
        on_dark: colours.poles.dark.value,
      });
    } else {
      setBrandColours({
        two_colour_mode: false,
        on_light: null,
        on_dark: null,
        shade_light: colours.poles.light.proposed_shade,
        shade_dark: colours.poles.dark.proposed_shade,
      });
    }
  };

  const modeToggle = (
    <label
      className={v.row}
      style={{ gap: 8, cursor: "pointer", flex: "none" }}
      data-mode-note
    >
      <Switch
        id="two-colour-mode"
        data-testid="two-colour-mode"
        checked={colours.two_colour_mode}
        onCheckedChange={setMode}
      />
      <span className={`${v.micro} ${v.dim}`}>two colours</span>
    </label>
  );

  return (
    <>
      <section data-group="Brand colour" className={v.stack}>
        {renderColour({
          label: "Brand colour",
          slot: "brand.canonical",
          calm: "The one colour. Good: 3 : 1 on both grounds.",
          value: brand.brand.canonical,
          flags: flagsFor("brand.canonical"),
          /**
           * A NEW CANONICAL DROPS THE LITERAL ADAPTATIONS — in one-colour mode,
           * where an adaptation is not a decision but a RESULT.
           *
           * "on-light/on-dark are SHADES OF THAT COLOUR, not free colours."
           * A literal `on_light` is the colour a shade PRODUCED, and keeping it
           * across a canonical edit pins every branded figure to the colour the
           * brand no longer has. Measured on the shipped kit, which stores
           * `on_light = on_dark = #f03541` as literals with both shades null:
           * moving canonical to #1e40af moved the branded ROOT ground to blue
           * and left every CTA, highlight and branded border painting #f03541.
           * That is "the preview isn't adapting, brand stays initial", and it is
           * one brand claiming one colour while storing three.
           *
           * THE SHADE AMOUNTS GO WITH THE POLES. The owner, 2026-09-06: "color
           * calculation can never be based on a calculation of another color."
           * An earlier version kept `shade_light`/`shade_dark` on the argument
           * that an authored shade is intent. Under his rule that argument is
           * wrong: 39 % toward black is an ANSWER ABOUT ONE COLOUR — the least
           * darkening that gets okuro's mint over the threshold — not a
           * statement about the brand. Carried onto a yellow it darkens a colour
           * that never had the problem. Dropping all four makes the engine
           * re-propose from the new canonical, which is the same thing
           * `fork_kit` does and the same thing a fresh brand does.
           *
           * Two-colour mode is the explicit opt-in to free colours, so there the
           * literals are the decision and are left alone. Same act `setMode`
           * already performs on the way back to one colour, and the same act
           * the GUESTS scene performs on a guest's canonical.
           */
          onChange: (value) =>
            setBrandColours(
              colours.two_colour_mode
                ? { canonical: value }
                : {
                    canonical: value,
                    on_light: null,
                    on_dark: null,
                    shade_light: null,
                    shade_dark: null,
                  },
            ),
          onReveal: onReveal ? () => onReveal("brand.canonical") : undefined,
          /* THE MODE SWITCH BELONGS TO THIS DECISION.
             It is the question "is this ONE colour" — a property of the colour
             above it, not a ninth decision. Where it SITS is the host's call:
             on the row for a rail that has 616px to spend, on a row of its own
             for a panel that describes its switches. */
          beside: modeToggle,
          besideHint: "Independent light and dark pole colors.",
        })}
      </section>

      {colours.two_colour_mode ? (
        <section data-group="On light and dark grounds" className={v.stack} data-mode="two-colour">
          {renderColour({
            label: "On light grounds",
            slot: "brand.on_light",
            calm: `Must sit below ${threshold} to stand on a light ground.`,
            value: brand.brand.on_light ?? colours.poles.light.value,
            flags: flagsFor("brand.on_light"),
            onChange: (value) => setBrandColours({ on_light: value }),
            onReveal: onReveal ? () => onReveal("brand.on_light") : undefined,
          })}
          {renderColour({
            label: "On dark grounds",
            slot: "brand.on_dark",
            calm: `Must sit above ${threshold} to stand on a dark ground.`,
            value: brand.brand.on_dark ?? colours.poles.dark.value,
            flags: flagsFor("brand.on_dark"),
            onChange: (value) => setBrandColours({ on_dark: value }),
            onReveal: onReveal ? () => onReveal("brand.on_dark") : undefined,
          })}
        </section>
      ) : (
        /* ONE GROUP, TWO CONTROLS. The adaptations are one decision — "how does
           this colour reach both grounds" — and they were two groups with a
           hairline between them, which said they were two. */
        <section data-group="Adaptations" className={v.stack}>
          <PoleShade
            pole="light"
            model={colours.poles.light}
            flags={flagsFor("brand.on_light")}
            v={v}
            onShade={(amount) => setBrandColours({ shade_light: amount, on_light: null })}
            onReveal={onReveal ? () => onReveal("brand.on_light") : undefined}
            renderHelp={renderHelp}
            helpOffersShade={helpOffersShade}
          />
          <PoleShade
            pole="dark"
            model={colours.poles.dark}
            flags={flagsFor("brand.on_dark")}
            v={v}
            onShade={(amount) => setBrandColours({ shade_dark: amount, on_dark: null })}
            onReveal={onReveal ? () => onReveal("brand.on_dark") : undefined}
            renderHelp={renderHelp}
            helpOffersShade={helpOffersShade}
          />
        </section>
      )}
    </>
  );
}
