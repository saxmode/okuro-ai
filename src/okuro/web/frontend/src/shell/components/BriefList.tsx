// SPDX-License-Identifier: Apache-2.0
/**
 * DAILY BRIEFS — the audio summaries, back in history, playable in place.
 *
 * The owner, 2026-09-16: *"Daily Briefs -> is a list of the audio summaries.
 * Audio summary is also missing. Daily briefs goes back in history. Directly
 * playable."*
 *
 * ===========================================================================
 * NOTHING HERE IS NEW MACHINERY
 * ===========================================================================
 * `peer.delivery.morning_brief_daemon` renders one MP3 a day at 06:00 and
 * registers it as a media asset tagged `brief`. `useMorningBrief` already asks
 * for it with `limit:1`; history is the identical call with a bigger limit. So
 * the "list" is one parameter, and what was actually missing after the cutover
 * was a PLAYER — `pulse-canvas.tsx` owned the only one and went unmounted.
 *
 * ===========================================================================
 * ONE <audio>, HOISTED — THE OLD CODE'S REASON STILL APPLIES
 * ===========================================================================
 * `brief-indicator.tsx` hoisted its element out of the dot with this note:
 * *"two <audio> elements and two `speaking` flags would mean entering
 * fullscreen mid-brief left the sidebar's player running with nothing able to
 * stop it."* One element per ROW would be the same defect with more copies —
 * play day 3, click day 5, and two briefs talk over each other with no way to
 * stop the first. So the element lives here, once, and the rows are buttons
 * that point it at a different `src`.
 *
 * `playing` is driven off the media element's own events, never off the click,
 * so it cannot drift from what is actually audible. That is the old file's rule
 * and it is kept verbatim.
 *
 * ===========================================================================
 * WHAT A ROW SAYS WHEN THERE IS NOTHING TO SAY
 * ===========================================================================
 * A brief is missing whenever the daemon did not run — 2026-09-16 has none
 * because the daemon was down when the cron fired. The list shows what EXISTS
 * and never invents a row for a silent day: an empty list says "no briefs yet",
 * a short list is a short list. Inferring "missing day" here would need the
 * daemon's schedule, which this component has no business knowing.
 */

import { useRef, useState } from "react";
import { Pause, Play } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { mediaApi, type MediaAsset } from "@/lib/assets-api";

/** Two weeks back. Enough to find "the one from Tuesday" without a scroll race. */
const HISTORY_LIMIT = 14;

/** Same key the daemon tags with (`peer.delivery.morning_brief_audio`). */
const BRIEF_TAG = "brief";

function dayLabel(iso: string | undefined): string {
  if (!iso) return "UNDATED";
  // `created_at` arrives as "YYYY-MM-DD HH:MM:SS" (SQLite), which `Date.parse`
  // reads as LOCAL time in every browser that matters — correct here, since the
  // brief is named for the morning the user woke up to.
  const d = new Date(iso.replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  const today = new Date();
  const days = Math.round((today.setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86_400_000);
  // THE TIME IS NOT DECORATION — measured 2026-09-16, the asset list holds TWO
  // briefs on several days (09-15 carries one at 06:02 and one at 08:46), so the
  // day alone printed "YESTERDAY" twice with no way to tell the rows apart.
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  if (days === 0) return `TODAY ${time}`;
  if (days === 1) return `YESTERDAY ${time}`;
  const day = d.toLocaleDateString(undefined, { weekday: "short", day: "2-digit", month: "2-digit" }).toUpperCase();
  return `${day} ${time}`;
}

export function BriefList() {
  const { data, isLoading } = useQuery({
    queryKey: ["assets", "morning-brief", "history", HISTORY_LIMIT],
    queryFn: () => mediaApi.list({ kind: "audio", tag: BRIEF_TAG, limit: HISTORY_LIMIT }),
    staleTime: 5 * 60_000,
  });

  const audioRef = useRef<HTMLAudioElement>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);
  // WHICH asset the element was last POINTED AT. The click writes this; the
  // media element's own `play` event is what promotes it to `playingId`. Keeping
  // the two apart is what stops the UI claiming "playing" for a file that 404s
  // or that autoplay policy refused.
  const armedRef = useRef<string | null>(null);

  const items: MediaAsset[] = data?.items ?? [];

  function toggle(asset: MediaAsset) {
    const el = audioRef.current;
    if (!el) return;
    if (armedRef.current === asset.id && !el.paused) {
      el.pause();
      return;
    }
    // A DIFFERENT ROW MEANS A NEW SOURCE, and `load()` is what makes the element
    // forget the previous one's buffered position. Without it, switching days
    // resumes the NEW file at the OLD file's currentTime.
    armedRef.current = asset.id;
    el.src = mediaApi.fileUrl(asset.id);
    el.load();
    void el.play().catch(() => {
      armedRef.current = null;
      setPlayingId(null);
    });
  }

  return (
    <div className="sb-briefs">
      <audio
        ref={audioRef}
        preload="none"
        onPlay={() => setPlayingId(armedRef.current)}
        onPause={() => setPlayingId(null)}
        onEnded={() => {
          armedRef.current = null;
          setPlayingId(null);
        }}
        aria-hidden="true"
      />
      {isLoading && <p className="sb-note">LOADING…</p>}
      {!isLoading && items.length === 0 && <p className="sb-note">NO BRIEFS YET</p>}
      {items.map((a) => {
        const on = playingId === a.id;
        return (
          <button
            key={a.id}
            type="button"
            className="sb-brief sh-ctl"
            aria-pressed={on}
            aria-label={`${on ? "Pause" : "Play"} the brief from ${dayLabel(a.created_at)}`}
            onClick={() => toggle(a)}
          >
            {on ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
            <span>{dayLabel(a.created_at)}</span>
          </button>
        );
      })}
    </div>
  );
}
