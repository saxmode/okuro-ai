import { useCallback, useRef, useState, type ReactNode } from "react";
import { mediaApi } from "@/lib/assets-api";
import { useMorningBrief } from "@/hooks/use-morning-brief";
import "./brief-indicator.css";

export interface BriefIndicatorState {
  /** The <audio> element itself. Render ONCE, somewhere that outlives the dots. */
  audio: ReactNode;
  hasBrief: boolean;
  /** okuro has something unheard to say. */
  unheard: boolean;
  /** okuro is audibly talking right now. */
  speaking: boolean;
  toggle: () => void;
}

/**
 * Brief playback state, hoisted out of the dot on purpose.
 *
 * The dot is drawn twice — once under the sidebar orb, once under the
 * fullscreen orb — and both are mounted at the same time (fullscreen is an
 * overlay, the sidebar keeps living behind it). Two <audio> elements and two
 * `speaking` flags would mean entering fullscreen mid-brief left the sidebar's
 * player running with nothing able to stop it. So the audio and the state live
 * here, at the canvas, and the dots are pure buttons over them.
 *
 * Speaking is driven off the media element's own events, never off the click,
 * so it cannot drift from what is actually audible.
 */
export function useBriefIndicator(): BriefIndicatorState {
  const { brief, unheard, markHeard } = useMorningBrief();
  const audioRef = useRef<HTMLAudioElement>(null);
  const [speaking, setSpeaking] = useState(false);

  const toggle = useCallback(() => {
    const el = audioRef.current;
    if (!brief || !el) return;
    if (!el.paused) {
      el.pause();
      el.currentTime = 0;
      return;
    }
    // Playing IS the "heard" signal — persist it before the audio resolves so
    // the glow quiets even if playback is later interrupted.
    markHeard(brief.id);
    el.play().catch(() => setSpeaking(false));
  }, [brief, markHeard]);

  const audio = brief ? (
    <audio
      ref={audioRef}
      src={mediaApi.fileUrl(brief.id)}
      preload="none"
      className="hidden"
      onPlay={() => setSpeaking(true)}
      onPause={() => setSpeaking(false)}
      onEnded={() => setSpeaking(false)}
    />
  ) : null;

  return { audio, hasBrief: !!brief, unheard, speaking, toggle };
}

/**
 * BriefDot — the "okuro would like to say something" dot.
 *
 * Two states, no chrome (sizes/colors in brief-indicator.css, keyed on
 * data-active + data-speaking):
 *   nothing to say  -> small, near-white, quiet
 *   brief unheard   -> larger, accent, glowing, pinging — okuro wants to talk
 *   brief playing   -> same, minus the ping (it is a stop button now)
 *
 * ALWAYS renders, even with no brief at all. That is deliberate: the dot is a
 * fixed landmark in the stack, so a brief arriving changes its appearance and
 * never reflows the text around it.
 *
 * Clicking plays through a controls-less <audio> — the only feedback is the orb
 * itself, which runs hot for exactly as long as playback lasts (see
 * PulseCanvas). Clicking again stops it and hands the orb back to real activity.
 */
export function BriefDot({
  state,
  hidden = false,
}: {
  state: BriefIndicatorState;
  hidden?: boolean;
}) {
  const { hasBrief, unheard, speaking, toggle } = state;

  // Big + accent while okuro has something to say AND while it is saying it —
  // the same bubble is the play and the stop control, so it must stay legible
  // as a target during playback rather than shrink away on the first click.
  const active = unheard || speaking;

  const label = !hasBrief
    ? "No brief yet"
    : speaking
      ? "Stop the brief"
      : unheard
        ? "Play the brief — new"
        : "Replay the brief";

  return (
    <div
      className={`pointer-events-auto flex justify-center transition-opacity duration-300 ${
        hidden ? "pointer-events-none opacity-0" : "opacity-100"
      }`}
    >
      <button
        type="button"
        // Stop the click here. In fullscreen this dot sits inside an overlay
        // whose own onClick exits fullscreen — without this, playing the brief
        // would slam the pulse shut on the same click that started it.
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
        disabled={!hasBrief}
        data-active={active}
        data-speaking={speaking}
        aria-label={label}
        title={hasBrief ? "okuro has something to say" : undefined}
        className="brief-indicator__dot rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
      />
    </div>
  );
}
