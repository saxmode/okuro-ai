// SPDX-License-Identifier: Apache-2.0
/**
 * THE PANEL'S ACTIVITY METER — `23 C · 5 A` and a bar per bucket.
 *
 * ===========================================================================
 * WHERE THIS COMES FROM, AND WHY IT IS A RESTORATION RATHER THAN AN INVENTION
 * ===========================================================================
 * The standalone shell prototype
 * (`projects/okuro-ui-redesign-prototype/index.html:26`, outside this repo)
 * ends the panel with one element the shell never built:
 *
 *     <div class="weather">23 C · 5 A <i aria-hidden="true"></i></div>
 *     .weather i { width:112px; height:18px;
 *       background: repeating-linear-gradient(90deg, fg 0 2px, transparent 2px 5px);
 *       clip-path: polygon(0 75%, 6% 50%, 10% 88%, 15% 22%, …) }
 *
 * The owner confirmed 2026-09-16 that `C` and `A` are the OLD PULSE PANEL'S
 * `CALLS` and `AGENTS` stat pair — the row `pulse-canvas.tsx:518-519` rendered
 * and the cutover dropped. So this is that stat pair, in the shape the redesign
 * gives it. The p1 confirmation mockup already lacked the strip, which means it
 * was never built rather than broken later.
 *
 * ===========================================================================
 * THE PROTOTYPE'S BARS ARE A PICTURE; THESE ARE THE DATA
 * ===========================================================================
 * `clip-path` with nineteen hand-written vertices is a mock — the same nineteen
 * spikes at every activity level, forever. The owner asked for the bar ANIMATION,
 * so the bars carry a real reading:
 *
 *   60 buckets x 500ms = the 30s the strip spans. Newest on the RIGHT, so it
 *   reads the way a timeline does. It was 24 x 2.5s across the full 60s window
 *   `use-activity-stream.ts` keeps (`CALL_WINDOW_MS = 60_000`), then 60 x 1s
 *   across that same minute; the constants below carry why it is now 500ms and
 *   why the COUNT, not the span, is what had to stay fixed.
 *
 * WHAT IS KEPT FROM THE PROTOTYPE is the GRAMMAR, because that is the design:
 * 2px bar, 3px gap, 18px tall — exactly its `0 2px, transparent 2px 5px` pitch.
 * 24 bars at that pitch is 117px against its 112px, i.e. the same strip, now
 * with a bucket count that means something.
 *
 * ===========================================================================
 * THREE THINGS THIS DELIBERATELY DOES NOT DO
 * ===========================================================================
 * ITEMS 1 AND 2 NO LONGER HOLD — the owner reversed both on 2026-09-19 (*"the
 * meter moves the bar and lets it grow again … the idea is only move"*). The
 * meter now owns a timer and normalises against a fixed ceiling, and the two
 * comments at `samples` and `bars` below carry the reasoning. They are kept
 * here because a reader who meets the ring needs to know these were arguments
 * that LOST, not oversights. Item 3 stands unchanged.
 *
 * 1. NO TIMER OF ITS OWN. The heights are a pure function of `activityStream`,
 *    which the hook already prunes every 2s. A second interval here would be a
 *    second clock disagreeing with the first about what "the last minute" is.
 *
 * 2. NO NORMALISATION AGAINST A FIXED CEILING. The tallest bucket in the window
 *    is full height, so the shape reads at one call a minute and at two hundred.
 *    An absolute ceiling would make every ordinary minute a flat line — which is
 *    the same "authored for one magnitude" mistake the engine's geometry just
 *    had.
 *
 * 3. NO FAKE ZERO. `activity === undefined` means no source has answered yet,
 *    and the numbers render as em-dashes — the contract `usePulseData` states
 *    at its return (*"so PulseCanvas can render em-dashes for 'no data' rather
 *    than a fake zero"*). An empty bar row is a real quiet minute; a dash is
 *    "we do not know".
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useShellPulse } from "./PulseData";

/* THE COLUMN COUNT IS THE FIXED SIDE OF THIS, NOT THE TIME SPAN — and that is
   a geometry constraint, not a preference. The strip is one flex row whose bars
   are `flex:1 1 0` with `min-width:1px` and a 1px gap (`shell.css`), so the
   count decides the bar width: at ~236px, sixty bars are ~2.9px each and one
   hundred and twenty would be under the 1px floor, i.e. the row would overflow
   its own box. So when the owner asked for a 500ms step (2026-09-19) the choice
   was between halving the bar width and halving the span, and he took the span:
   sixty columns, thirty seconds.

   WHAT THIS REPLACES: one column per second across a full minute, which came
   from *"the agent activity monitor needs to jump one line to the left per
   second. like nvtop or btop"* — the STEPPING is what that ruling was about,
   and it survives; only its rate moved. Before that it was 24 buckets of 2.5s,
   which could not step once a second whatever drove it.

   NOTE THE NUMBERS ABOVE THE STRIP STILL SPAN A MINUTE. `activity.calls` comes
   from the hook's own `CALL_WINDOW_MS = 60_000` and is untouched by this; the
   strip is the only thing that now shows thirty seconds. The `aria-label` names
   the numbers, and the bars are `aria-hidden`, so the two spans do not collide
   in what a screen reader is told. */
const BUCKET_MS = 500;
const BUCKETS = 60; // x BUCKET_MS = the 30s the strip spans, end to end
/* FIVE, BECAUSE THE COLUMN HAS FIVE SQUARES — and a scale finer than the thing
   drawing it cannot be read. `shell.css` derives the row count from the two
   tokens rather than typing it:

     rows = (bar height + gap) / (square + gap) = (18 + 2) / (2 + 2) = 5

   At a ceiling of ten, one agent and two agents both landed inside the first
   square, which is what the owner saw: *"the agent-meter has a height of 5
   elements. therefore 2 agents stay the same height. max-should 5 agents."*
   At five, an agent IS a square — the reading and the drawing share one unit,
   and no rounding stands between them. */
const AGENT_CEILING = 5;

export function ActivityMeter() {
  const { activity } = useShellPulse();

  /* THE STRIP IS A RING OF SAMPLES, AND THAT IS WHY IT ONLY MOVES.
     It used to recompute every column from `activityStream` and normalise them
     against the window's own peak, so one busy second RESCALED all sixty —
     every bar grew or shrank as the peak moved. The owner: *"the meter moves the
     bar and lets it grow again so it's move-grow. The idea is only move."*

     A ring fixes that by construction: each tick pushes ONE new reading and
     drops the oldest. A column's value is decided once, when it is written, and
     never touched again — so the only thing that can happen to it afterwards is
     to move left.

     WHY `activity.live` AND NOT THE STREAM. The owner asked for AGENTS, and
     `ActivityStreamEntry` is `{ts, tool?, preview?}` — it carries no agent
     identity, so a per-second agent count cannot be derived from it. `live` is
     the agent count the same hook already publishes, and a once-a-second sample
     of it IS the history he described. */
  const [samples, setSamples] = useState<number[]>(() => new Array<number>(BUCKETS).fill(0));
  const liveRef = useRef(0);
  liveRef.current = activity?.live ?? 0;

  useEffect(() => {
    const id = window.setInterval(() => {
      setSamples((prev) => [...prev.slice(1), liveRef.current]);
    }, BUCKET_MS);
    return () => window.clearInterval(id);
  }, []);

  /* A FIXED CEILING, WHICH THIS FILE USED TO ARGUE AGAINST. The old comment
     said an absolute ceiling would "make every ordinary minute a flat line" and
     preferred the window's peak. That is exactly what produced the growing —
     and the owner ruled the scale: *"maximum is 10 agents. 10 = bar full
     height."* A fixed ceiling is also the only scale on which two different
     minutes mean the same thing. */
  const bars = useMemo(
    () => samples.map((n) => (n <= 0 ? 0 : Math.min(1, n / AGENT_CEILING))),
    [samples],
  );

  const calls = activity ? String(activity.calls) : "—";
  const agents = activity ? String(activity.live) : "—";

  return (
    <div
      className="sb-meter"
      /* ONE LABEL FOR THE WHOLE THING. The bars are the same reading drawn
         twice, so they stay `aria-hidden` and the text carries the meaning —
         the prototype's own `aria-hidden` on its `<i>`, kept. */
      aria-label={`${calls} tool calls and ${agents} live agents in the last minute`}
    >
      <span className="sb-meter-n">
        {calls}
        <i>C</i>
        <em>·</em>
        {agents}
        <i>A</i>
      </span>
      <span className="sb-meter-bars" aria-hidden="true">
        {bars.map((v, i) => (
          <b key={i} style={{ ["--v" as string]: v }} />
        ))}
      </span>
    </div>
  );
}
