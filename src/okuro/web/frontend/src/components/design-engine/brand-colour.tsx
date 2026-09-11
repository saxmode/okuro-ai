/**
 * THE BRAND COLOUR — decisions one, two and three.
 *
 *     "ONE BRAND = ONE COLOUR, DEFAULT: canonical is chosen, and on-light/
 *      on-dark are SHADES OF THAT COLOUR (adjustment flow), not free colours.
 *      A brand genuinely using two different colours must TOGGLE into that mode
 *      explicitly."
 *
 * So one colour picker, then two shade controls. The engine's proposal is drawn
 * on the same track the shade moves along, and — this is the change — it is a
 * LABELLED CONTROL rather than a tooltip: a visible tick, the number printed
 * beside it, and a button that takes it. Measured before, the proposal was a
 * 1 × 16px line whose only affordance was a hover, sitting next to a slider
 * thumb that scored 1.00 : 1 against the page it was drawn on. The most useful
 * number the engine computes was the least visible thing on the rail.
 *
 * NOTHING HERE COMPUTES A COLOUR. The slider sends an AMOUNT and the engine
 * returns the colour, which is why the amount is what gets stored: a page that
 * shaded the hex itself would be a second implementation of the adjustment,
 * diverging exactly at the threshold where it matters.
 */

import { useEffect, useRef, useState } from "react";
import { Slider } from "radix-ui";
import { Switch } from "@/components/ui/switch";
import { HelpSlot } from "./advice";
import { ColourField } from "./fields";
import {
  POLARITY_THRESHOLD,
  adviceFor,
  severityOf,
  type AdviceAction,
} from "./severity";
import type { BrandColourModel, BrandJson, BrandPole, Flag } from "./types";

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
  onChange,
}: {
  label: string;
  amount: number;
  proposed: number;
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
            background: "var(--ce-border)",
          }}
        >
          <Slider.Range
            className="absolute"
            style={{ height: "100%", borderRadius: 2, background: "var(--ce-fg-2)" }}
          />
        </Slider.Track>
        {/* 16px, a 1px border and a shadow. Measured before: 14px, a
            TRANSPARENT border and `bg-background` — 1.00 : 1 against the page,
            which is a thumb you cannot see at all. */}
        {/* Geometry and ink live in chrome.css: 16px of paint inside a 24px
            target, so "every interactive element is at least 24 x 24" and "the
            thumb reads as a 16px dot" are both true of the same element. An
            inline style here would win over that rule and quietly reinstate a
            14px thumb. */}
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
          background: "var(--ce-fg-2)",
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
  colours,
  onShade,
  onReveal,
  onAction,
}: {
  pole: "light" | "dark";
  model: BrandPole;
  flags: Flag[];
  colours: BrandColourModel;
  onShade: (amount: number) => void;
  onReveal: () => void;
  onAction: (action: AdviceAction, flag: Flag) => void;
}) {
  // A pole authored as a COLOUR before the shade model existed has no amount.
  // Seeding the slider with the engine's proposal would move a value the brand
  // never asked to move, so it starts at 0.
  const amount = model.shade ?? 0;
  const percent = Math.round(amount * 100);
  const proposed = Math.round(model.proposed_shade * 100);
  const label = pole === "light" ? "On light grounds" : "On dark grounds";
  const toward = pole === "light" ? "black" : "white";

  /* `HelpSlot` renders the WORST finding and only that one, so the question
     "does the note already offer this shade" is asked of the same flag the
     reader will see — not of the set. */
  const worst = flags
    .slice()
    .sort(
      (a, b) =>
        (severityOf(b) === "must-fix" ? 3 : severityOf(b) === "decide" ? 2 : 1) -
        (severityOf(a) === "must-fix" ? 3 : severityOf(a) === "decide" ? 2 : 1),
    )[0];
  const noteOffersShade = Boolean(
    worst &&
      adviceFor(worst, colours).actions.some((a) => a.kind === "set-shade"),
  );

  return (
    /* NOT ITS OWN GROUP ANY MORE — the two poles share one (his item 9).
       "On light grounds" and "On dark grounds" are the SAME question asked twice
       about one colour, so they are two controls of one decision rather than two
       decisions. Measured before: 181px each, and a 32px group gap plus a
       hairline between them for a boundary that was not a real one. */
    <div data-control={label} data-pole={pole} data-mode="one-colour" className="ce-stack-tight">
        <div className="ce-row" style={{ gap: 8 }}>
          <button
            type="button"
            className="ce-label"
            onClick={onReveal}
            style={{
              background: "transparent",
              border: 0,
              padding: 0,
              minHeight: 24,
              color: "var(--ce-fg)",
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
          <span className="ce-row" style={{ marginLeft: "auto", gap: 4 }}>
            {/* THE READOUT IS THE AMOUNT AND NOTHING ELSE — `/^\d{1,3} %$/`.
                The colour it produces rides on the same ROW, in its own
                element, because one decision deserves one row; folding the hex
                into this span would make the one place the rail reports a
                quantity report two things. */}
            <span className="ce-value" data-shade-reads>
              {percent} %
            </span>
            <span
              aria-hidden
              data-swatch={`on-${pole}`}
              className="ce-swatch"
              style={{ background: model.value }}
            />
            <span className="ce-value" style={{ fontFamily: "var(--font-mono)" }}>
              {model.value}
            </span>
          </span>
        </div>

        <ShadeSlider
          label={`${label} shade`}
          amount={amount}
          proposed={model.proposed_shade}
          onChange={onShade}
        />

        {/* THE PROPOSAL AS A LABELLED CONTROL — and ONLY when no note is
            offering the same act. `[Use 32 %]` here and `[Set 32 %]` in the note
            130px below were two buttons for one edit, in one group, and the
            reader's question was "which do I press". The note owns it when
            there is a note that performs it; the row owns it otherwise, and it
            carries the same `data-use-proposal` either way. */}
        {percent !== proposed && !noteOffersShade && (
          <button
            type="button"
            className="ce-chip"
            data-use-proposal={pole}
            style={{ alignSelf: "flex-start" }}
            onClick={() => onShade(model.proposed_shade)}
          >
            Use {proposed} %
          </button>
        )}

        <HelpSlot
          calm={
            model.shade == null
              ? `Authored as a colour. Good: a shade that crosses ${POLARITY_THRESHOLD} — try ${proposed} %.`
              : `Toward the brand's own ${toward}. Good: the ${proposed} % that crosses ${POLARITY_THRESHOLD}.`
          }
          flags={flags}
          colours={colours}
          onAction={onAction}
        />
    </div>
  );
}

/* ----------------------------------------------------------------- section */

export function BrandColourSection({
  brand,
  colours,
  flagsFor,
  onChange,
  onReveal,
  onAction,
}: {
  brand: BrandJson;
  colours: BrandColourModel;
  flagsFor: (slot: string) => Flag[];
  onChange: (next: BrandJson) => void;
  onReveal: (slot: string) => void;
  onAction: (action: AdviceAction, flag: Flag) => void;
}) {
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

  return (
    <>
      <section data-group="Brand colour" className="ce-stack">
        <ColourField
          label="Brand colour"
          calm="The one colour. Good: 3 : 1 on both grounds."
          value={brand.brand.canonical}
          flags={flagsFor("brand.canonical")}
          colours={colours}
          onAction={onAction}
          /**
           * A NEW CANONICAL DROPS THE LITERAL ADAPTATIONS — in one-colour mode,
           * where an adaptation is not a decision but a RESULT.
           *
           * "on-light/on-dark are SHADES OF THAT COLOUR, not free colours."
           * A shade is authored intent and survives: 40 % toward black stays
           * 40 % toward black and re-shades whatever the new colour is. A
           * literal `on_light` is the colour that shade PRODUCED, and keeping it
           * across a canonical edit pins every branded figure to the colour the
           * brand no longer has. Measured on the shipped kit, which stores
           * `on_light = on_dark = #f03541` as literals with both shades null:
           * moving canonical to #1e40af moved the branded ROOT ground to blue
           * and left every CTA, highlight and branded border painting #f03541.
           * That is "the preview isn't adapting, brand stays initial", and it is
           * one brand claiming one colour while storing three.
           *
           * Two-colour mode is the explicit opt-in to free colours, so there the
           * literals are the decision and are left alone. Same act `setMode`
           * already performs on the way back to one colour, and the same act
           * the GUESTS scene performs on a guest's canonical.
           */
          /* THE SHADE AMOUNTS GO WITH THE POLES. The owner, 2026-09-06: "color
             calculation can never be based on a calculation of another color."
             This dropped `on_light`/`on_dark` and KEPT `shade_light`/
             `shade_dark`, on the argument above that an authored shade is
             intent and survives an edit. Under his rule that argument is
             wrong: 39 % toward black is an ANSWER ABOUT ONE COLOUR — the
             least darkening that gets okuro's mint over the threshold — not a
             statement about the brand. Carried onto a yellow it darkens a
             colour that never had the problem. Dropping all four makes the
             engine re-propose from the new canonical, which is the same thing
             `fork_kit` does and the same thing a fresh brand does. */
          onChange={(value) =>
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
            )
          }
          onReveal={() => onReveal("brand.canonical")}
          /* THE MODE SWITCH RIDES ON THIS DECISION'S OWN ROW.
             It is the question "is this ONE colour" — a property of the colour
             above it, not a ninth decision — and it shipped as a row of its own
             plus a 40px paragraph restating it: 64px of a 616px rail for a
             control whose own label already says what it does. */
          beside={
            <label
              className="ce-row"
              style={{ gap: 8, cursor: "pointer", flex: "none" }}
              data-mode-note
            >
              <Switch
                id="two-colour-mode"
                data-testid="two-colour-mode"
                checked={colours.two_colour_mode}
                onCheckedChange={setMode}
              />
              <span className="ce-micro ce-2">two colours</span>
            </label>
          }
        />

      </section>

      {colours.two_colour_mode ? (
        <section data-group="On light and dark grounds" className="ce-stack" data-mode="two-colour">
          <ColourField
            label="On light grounds"
            calm={`Must sit below ${POLARITY_THRESHOLD} to stand on a light ground.`}
            value={brand.brand.on_light ?? colours.poles.light.value}
            flags={flagsFor("brand.on_light")}
            colours={colours}
            onAction={onAction}
            onChange={(value) => setBrandColours({ on_light: value })}
            onReveal={() => onReveal("brand.on_light")}
          />
          <ColourField
            label="On dark grounds"
            calm={`Must sit above ${POLARITY_THRESHOLD} to stand on a dark ground.`}
            value={brand.brand.on_dark ?? colours.poles.dark.value}
            flags={flagsFor("brand.on_dark")}
            colours={colours}
            onAction={onAction}
            onChange={(value) => setBrandColours({ on_dark: value })}
            onReveal={() => onReveal("brand.on_dark")}
          />
        </section>
      ) : (
        /* ONE GROUP, TWO CONTROLS. The adaptations are one decision — "how does
           this colour reach both grounds" — and they were two groups with a
           hairline between them, which said they were two. */
        <section data-group="Adaptations" className="ce-stack">
          <PoleShade
            pole="light"
            model={colours.poles.light}
            flags={flagsFor("brand.on_light")}
            colours={colours}
            onShade={(amount) => setBrandColours({ shade_light: amount, on_light: null })}
            onReveal={() => onReveal("brand.on_light")}
            onAction={onAction}
          />
          <PoleShade
            pole="dark"
            model={colours.poles.dark}
            flags={flagsFor("brand.on_dark")}
            colours={colours}
            onShade={(amount) => setBrandColours({ shade_dark: amount, on_dark: null })}
            onReveal={() => onReveal("brand.on_dark")}
            onAction={onAction}
          />
        </section>
      )}
    </>
  );
}
