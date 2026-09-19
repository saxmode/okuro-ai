/**
 * THE MOTION SPEED SCALE — the five authored durations, page-neutral.
 *
 * A brand authors five speeds (`xfast`/`fast`/`medium`/`slow`/`xslow`, memory
 * 989a0cd3) and `emit.py` reads them from the KIT, never from the system. They
 * are the one authored group with no editor on DESIGN: its Motion section
 * authors `default_speed`, `default_curve` and `signature` — three selects over
 * keys that already exist — while the VALUES behind those keys lived only in
 * `components/design-engine/rail.tsx`, behind Advanced › Motion.
 *
 * ── WAVE 6, 2026-09-14 ─────────────────────────────────────────────────────
 * the owner ruled option (a) on todo f6aeb2c8 (memory 102dcb37): the scale gets
 * an editor on DESIGN rather than being retired. Moved here whole; the old rail
 * imports it back, so the two pages cannot drift apart for the days the old one
 * has left, and `/design-engine`'s deletion takes no capability with it.
 *
 * THE WRITE IS THE WHOLE MAP — the shape `EffectIntensities` already uses, and
 * the shape this folder's rule asks for. It removes a question rather than
 * answering it: nothing here has to know which `setPath` dialect its host runs.
 *
 * AND IT IS NOT, HERE, A CORRECTNESS FIX — measured, because the obvious claim
 * is wrong. Gotcha d1911006 is about ARRAYS: `rail.tsx::setPath` special-cases
 * `Array.isArray(node)` and DESIGN's does not, so `border.weight_factors.1`
 * yields a tuple on one page and `{"0":…,"1":…}` on the other. For an OBJECT
 * node both dialects do `{ ...node, [key]: next }`, which preserves the keys and
 * their order — so a dotted write into `motion.speeds` is byte-identical to a
 * whole-map write. Verified by breaking DESIGN to write the dotted path and
 * re-running `motion-fields-both-rails.test.tsx`: all five cases stayed green.
 * The whole-map shape is still correct and still what ships; what it is not is a
 * defence against a divergence that exists at this address. Claiming otherwise
 * would put a guard in the comments that no gate can fail on.
 *
 * A SPEED IS MILLISECONDS, NOT A FACTOR — which is why this is not a
 * `FactorField`. Sizes show px and save factors because the base can move
 * underneath them; a duration has no base and `schema` stores exactly the number
 * authored. Showing px-style arithmetic here would invent a base that does not
 * exist.
 *
 * WHAT THIS EDITOR MUST NEVER BE USED FOR, his ruling of 2026-08-19 (memory
 * 92c02890): these speeds drive ANIMATIONS. Base interaction transitions —
 * hover, focus, press — run at `lib/interaction.ts::INTERACTION_MS`, a
 * non-authored constant, and must not reference this map at all. That is the
 * subject of `test_showcase::test_base_interactions_never_run_at_an_authored_
 * motion_speed`, which re-authors this scale in order to prove the interaction
 * timing does NOT follow it.
 */

import { useEffect, useRef, useState } from "react";

import { Input } from "@/components/ui/input";
import type { AuthoringVocabulary } from "./vocabulary";

/**
 * One speed, in milliseconds.
 *
 * The draft is local so every keystroke appears, and the authoritative value
 * overwrites it only when it moves for a reason OTHER than this field — the
 * same rule `FactorField` follows, for the same reason: a debounced round trip
 * otherwise snaps the input back between characters.
 */
function SpeedField({
  name,
  ms,
  vocabulary: v,
  onChange,
}: {
  name: string;
  ms: number;
  vocabulary: AuthoringVocabulary;
  onChange: (nextMs: number) => void;
}) {
  const shown = String(ms);
  const [draft, setDraft] = useState(shown);
  const [error, setError] = useState<string | null>(null);
  const sent = useRef(shown);

  useEffect(() => {
    if (shown !== sent.current) {
      sent.current = shown;
      setDraft(shown);
      setError(null);
    }
  }, [shown]);

  const commit = () => {
    const next = Number(draft);
    if (!Number.isFinite(next) || next < 0) {
      setError("A speed is a number of milliseconds, never below zero.");
      return;
    }
    setError(null);
    sent.current = String(next);
    setDraft(String(next));
    if (next !== ms) onChange(next);
  };

  return (
    <div
      className={v.stackTight}
      style={{ gap: 4 }}
      /* THE STORED VALUE, PUBLISHED — a gate reads the number off the element
         rather than parsing the label or trusting the model. */
      data-motion-speed={name}
      data-ms={ms}
    >
      <span className={v.label}>{name}</span>
      <Input
        type="number"
        /* THE SPEED'S OWN NAME IS THE ACCESSIBLE NAME. It is how the engine
           addresses it and how the showcase gate finds the one it re-authors. */
        aria-label={name}
        aria-invalid={error ? true : undefined}
        value={draft}
        step={50}
        min={0}
        className={`${v.control} w-full`}
        style={{ fontFamily: "var(--font-mono)" }}
        onChange={(e) => {
          setDraft(e.target.value);
          if (error) setError(null);
        }}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          if (e.key === "Escape") {
            setDraft(shown);
            setError(null);
          }
        }}
      />
      {error && (
        <small className={`${v.micro} ${v.dim}`} role="alert">
          {error}
        </small>
      )}
    </div>
  );
}

/**
 * The authored speed scale — every speed the BRAND carries, in the brand's own
 * order.
 *
 * THE NAMES COME FROM THE BRAND, not from a list typed here, for the same reason
 * `EffectIntensities` takes its names from the effects map: a brand that grows a
 * sixth speed renders without this file changing, and an editor that enumerated
 * the five would silently stop showing whatever the engine learned next.
 *
 * ORDER IS NOT SORTED HERE. `kits.ratified_motion()` normalises the ladder into
 * `MOTION_SPEED_ORDER` on the way in and the server's validator checks it rises
 * (memory cb617f44); re-sorting in the editor would hide a rising-ladder
 * refusal the author needs to see. Flags never reject — the server states the
 * problem, the field keeps the number.
 */
export function MotionSpeeds({
  speeds,
  vocabulary: v,
  onChange,
}: {
  speeds: Record<string, number>;
  vocabulary: AuthoringVocabulary;
  onChange: (next: Record<string, number>) => void;
}) {
  return (
    <div className={v.stackTight} data-section="motion-speeds">
      {Object.entries(speeds).map(([name, ms]) => (
        <SpeedField
          key={name}
          name={name}
          ms={ms}
          vocabulary={v}
          onChange={(next) => onChange({ ...speeds, [name]: next })}
        />
      ))}
    </div>
  );
}
