import { useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { ArrowLeft, Check, Loader2, Pause, Play, Save } from "lucide-react";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/segmented";
import { SectionLabel } from "@/components/ui/section-label";
import { Switch } from "@/components/ui/switch";
import { toast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import {
  ttsApi,
  type TtsConfig,
  type TtsGender,
  type TtsVoiceOption,
} from "@/lib/api";

/**
 * /settings/tts — the two voices okuro speaks with.
 *
 * One model, mirroring the store (~/.okuro/tts-config.yaml via /api/voice/tts):
 * a standard female voice, a standard male voice, and which of the two IS okuro.
 * Roles are derived server-side — okuro narrates and hosts, the other voice
 * co-hosts — so a podcast always uses both and there is nothing per-role to pick.
 *
 * Only the roster this machine's tier actually renders with is offered (pro →
 * the Qwen speakers, air/advanced → Kokoro): the engine follows the hardware, so
 * listing the others was listing voices that will never be spoken.
 *
 * Every voice auditions inline against a prerendered clip of okuro's greeting —
 * rendered through the same seam a brief is, so a clip carries okuro's house
 * post-fx exactly as the real thing will, and turning that off re-renders the
 * roster rather than auditioning a sound okuro no longer makes. Qwen's roster
 * rides ONE model load (~45s for nine), which is too long to block a page on, so
 * the batch runs server-side and this polls for readiness.
 */

const POLL_MS = 4000;

// Shared <audio> so only one preview plays at a time; object URLs revoked on swap.
function usePreviewPlayer() {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);

  useEffect(() => {
    const el = new Audio();
    el.onended = () => setPlaying(null);
    audioRef.current = el;
    return () => {
      el.pause();
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    };
  }, []);

  const play = async (engine: string, voice: string) => {
    const el = audioRef.current;
    if (!el) return;
    if (playing === voice) {
      el.pause();
      setPlaying(null);
      return;
    }
    try {
      el.pause();
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      const url = await ttsApi.previewObjectUrl(engine, voice);
      urlRef.current = url;
      el.src = url;
      setPlaying(voice);
      await el.play();
    } catch (e) {
      setPlaying(null);
      toast.error(`No preview for ${voice}`, {
        description: String((e as Error)?.message ?? e),
      });
    }
  };

  return { play, playing };
}

/**
 * One auditionable voice. The card body selects; the play control auditions.
 * Two jobs, two targets — clicking a name to select shouldn't also make noise,
 * and hearing a voice shouldn't commit you to it.
 */
function VoiceCard({
  voice,
  selected,
  isOkuro,
  playing,
  onSelect,
  onPlay,
}: {
  voice: TtsVoiceOption;
  selected: boolean;
  isOkuro: boolean;
  playing: boolean;
  onSelect: () => void;
  onPlay: () => void;
}) {
  return (
    <div
      role="radio"
      aria-checked={selected}
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      className={cn(
        "group flex cursor-pointer items-center gap-2.5 rounded-md border px-2.5 py-2 transition-fast",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
        selected
          ? "border-accent bg-accent-subtle"
          : "border-border-subtle bg-surface hover:border-accent/50 hover:bg-surface-elevated",
      )}
    >
      <button
        type="button"
        aria-label={`Preview ${voice.label}`}
        disabled={!voice.has_preview}
        onClick={(e) => {
          e.stopPropagation();
          onPlay();
        }}
        className={cn(
          "inline-flex size-6 shrink-0 items-center justify-center rounded-full border transition-fast",
          voice.has_preview
            ? "border-border text-fg-secondary hover:border-accent hover:text-accent"
            : "border-border-subtle text-fg-subtle opacity-40",
        )}
      >
        {playing ? <Pause size={11} /> : <Play size={11} className="ml-px" />}
      </button>
      <span
        className={cn(
          "min-w-0 flex-1 truncate text-xs",
          selected ? "text-fg-primary font-medium" : "text-fg-secondary",
        )}
      >
        {voice.label}
      </span>
      {selected && (
        <span className="shrink-0 text-2xs font-medium uppercase tracking-wider text-accent">
          {isOkuro ? "okuro" : "co-host"}
        </span>
      )}
      {selected && <Check size={12} className="shrink-0 text-accent" />}
    </div>
  );
}

function VoiceColumn({
  gender,
  voices,
  engine,
  selected,
  isOkuro,
  player,
  onSelect,
}: {
  gender: TtsGender;
  voices: TtsVoiceOption[];
  engine: string;
  selected: string;
  isOkuro: boolean;
  player: ReturnType<typeof usePreviewPlayer>;
  onSelect: (id: string) => void;
}) {
  return (
    <section className="flex min-w-0 flex-col gap-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <SectionLabel>{gender === "female" ? "Female" : "Male"} voice</SectionLabel>
        <span className="text-2xs text-fg-subtle">
          {isOkuro ? "okuro speaks with this" : "co-hosts podcasts"}
        </span>
      </div>
      <div
        role="radiogroup"
        aria-label={`${gender} voice`}
        className="flex max-h-80 flex-col gap-1.5 overflow-y-auto pr-1"
      >
        {voices.map((v) => (
          <VoiceCard
            key={v.id}
            voice={v}
            selected={v.id === selected}
            isOkuro={isOkuro}
            playing={player.playing === v.id}
            onSelect={() => onSelect(v.id)}
            onPlay={() => void player.play(engine, v.id)}
          />
        ))}
      </div>
    </section>
  );
}

function PreviewBanner({ cfg }: { cfg: TtsConfig }) {
  const done = cfg.previews.total - cfg.previews.missing.length;
  return (
    <div className="flex items-center gap-2 rounded-md border border-border-subtle bg-surface-subtle px-3 py-2 text-xs text-fg-secondary">
      <Loader2 size={12} className="animate-spin text-accent" />
      <span>
        Rendering voice previews — {done}/{cfg.previews.total} ready.
      </span>
      <span className="text-fg-subtle">
        {cfg.engine === "qwen"
          ? "The whole roster renders on one model load; about a minute."
          : "A few seconds."}
      </span>
    </div>
  );
}

export function SettingsTtsPage() {
  const qc = useQueryClient();
  const player = usePreviewPlayer();
  const [draft, setDraft] = useState<TtsConfig | null>(null);
  const kicked = useRef(false);

  const info = useQuery({
    queryKey: ["tts", "config"],
    queryFn: ttsApi.getConfig,
    // Only while clips are still missing — a settings page has no reason to
    // poll once there is nothing left to wait for.
    refetchInterval: (q) => (q.state.data?.previews.ready === false ? POLL_MS : false),
  });

  // Seed the editable draft once, then keep server-owned facts (roster, preview
  // readiness) live under it without stomping in-flight edits.
  useEffect(() => {
    if (!info.data) return;
    setDraft((d) =>
      d
        ? { ...d, voices: info.data.voices, previews: info.data.previews }
        : info.data,
    );
  }, [info.data]);

  // Missing previews are the page's problem to solve, not the user's: kick the
  // batch once on arrival rather than shipping a "generate" button that only
  // ever has one right answer.
  useEffect(() => {
    if (!info.data || info.data.previews.ready || kicked.current) return;
    kicked.current = true;
    ttsApi.generatePreviews().catch(() => {
      /* a preview is a convenience — never block the page on it */
    });
  }, [info.data]);

  const save = useMutation({
    mutationFn: ttsApi.setConfig,
    onSuccess: (res) => {
      // Toggling the house sound invalidates every clip (a preview has to sound
      // like the render), so release the once-per-visit guard and let the effect
      // below re-kick the batch — otherwise the roster stays mute until reload.
      if (!res.previews.ready) kicked.current = false;
      setDraft(res);
      qc.setQueryData(["tts", "config"], res);
      toast.success("Voices saved", {
        description: res.previews.ready
          ? "Applies to your next brief or podcast."
          : "Re-rendering previews in the new sound.",
      });
    },
    onError: (e: unknown) => toast.error(String((e as Error)?.message ?? e)),
  });

  if (info.isLoading || !draft) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-fg-tertiary">
        <Loader2 size={14} className="mr-2 animate-spin" />
        Loading voice settings…
      </div>
    );
  }
  if (info.error || !info.data) {
    return <div className="p-6 text-sm text-error">Failed to load voice settings.</div>;
  }

  const female = draft.voices.filter((v) => v.gender === "female");
  const male = draft.voices.filter((v) => v.gender === "male");
  const okuroVoice = draft.okuro_gender === "female" ? draft.female : draft.male;
  const okuroLabel =
    draft.voices.find((v) => v.id === okuroVoice)?.label ?? okuroVoice;
  const dirty =
    draft.female !== info.data.female ||
    draft.male !== info.data.male ||
    draft.okuro_gender !== info.data.okuro_gender ||
    draft.speed !== info.data.speed ||
    draft.fx_enabled !== info.data.fx_enabled;

  const set = <K extends keyof TtsConfig>(k: K, v: TtsConfig[K]) =>
    setDraft((d) => (d ? { ...d, [k]: v } : d));

  return (
    <div className="flex max-w-3xl flex-col gap-6 p-6">
      <PageHeader
        title="Voices"
        subtitle={`Every voice below says: “${draft.sample_text}”`}
        right={
          <div className="flex items-center gap-3">
            {/* The roster follows the hardware, not a preference — so say so
                rather than let it look like voices went missing. */}
            <span
              title={`okuro-${draft.edition} renders audio with ${draft.engine}`}
              className="rounded bg-surface-subtle px-1.5 py-0.5 font-mono text-2xs text-fg-tertiary"
            >
              {draft.edition} · {draft.engine}
            </span>
            <Link
              to="/settings"
              className="flex items-center gap-1 text-xs text-fg-tertiary hover:text-fg-primary"
            >
              <ArrowLeft size={12} />
              Back to Settings
            </Link>
          </div>
        }
      />

      {/* Which voice is okuro — the one switch the whole model turns on. */}
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-surface-elevated p-4">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-fg-primary">okuro speaks with the…</h2>
          <p className="mt-1 text-xs text-fg-tertiary">
            okuro narrates your briefs and hosts your podcasts as{" "}
            <span className="text-fg-secondary">{okuroLabel}</span>. The other
            voice co-hosts, so a podcast is always two people.
          </p>
        </div>
        <Segmented<TtsGender>
          ariaLabel="Which voice is okuro"
          value={draft.okuro_gender}
          onChange={(g) => set("okuro_gender", g)}
          options={[
            { label: "Female voice", value: "female" },
            { label: "Male voice", value: "male" },
          ]}
        />
      </section>

      {!draft.previews.ready && <PreviewBanner cfg={draft} />}

      {/* The two standard voices. Gender-partitioned, so they can never collide. */}
      <div className="grid gap-6 sm:grid-cols-2">
        <VoiceColumn
          gender="female"
          voices={female}
          engine={draft.engine}
          selected={draft.female}
          isOkuro={draft.okuro_gender === "female"}
          player={player}
          onSelect={(id) => set("female", id)}
        />
        <VoiceColumn
          gender="male"
          voices={male}
          engine={draft.engine}
          selected={draft.male}
          isOkuro={draft.okuro_gender === "male"}
          player={player}
          onSelect={(id) => set("male", id)}
        />
      </div>

      {/* House sound. Not a per-tier setting: the fx chain is applied at the
          synth seam, so this one switch governs every voice okuro renders. */}
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-surface-elevated p-4">
        <div className="min-w-0">
          <label
            htmlFor="voice-fx"
            className="cursor-pointer text-sm font-semibold text-fg-primary"
          >
            House sound
          </label>
          <p className="mt-1 max-w-xl text-xs text-fg-tertiary">
            okuro's voice processing — the EQ, chorus and room that make every
            voice above sound like the same studio. Off ships them raw, exactly
            as the model rendered them.
          </p>
        </div>
        <div className="flex items-center gap-2.5">
          <span className="text-2xs text-fg-tertiary">
            {draft.fx_enabled ? "On" : "Off"}
          </span>
          <Switch
            id="voice-fx"
            checked={draft.fx_enabled}
            onCheckedChange={(v) => set("fx_enabled", v)}
          />
        </div>
      </section>

      {/* Pace */}
      <section className="flex flex-col gap-2 rounded-md border border-border bg-surface-elevated p-4">
        <label className="flex items-center gap-3 text-sm">
          <span className="w-16 text-fg-secondary">Pace</span>
          <input
            type="range"
            min={draft.speed_min}
            max={draft.speed_max}
            step={0.05}
            value={draft.speed}
            onChange={(e) => set("speed", Number(e.target.value))}
            className="flex-1 accent-accent"
          />
          <span className="w-20 text-right font-mono text-xs text-fg-primary">
            {draft.speed === 1 ? "1.0× (off)" : `${draft.speed.toFixed(2)}×`}
          </span>
        </label>
        <p className="text-2xs text-fg-tertiary">
          Off by default — time-stretching preserves pitch but costs naturalness.
        </p>
      </section>

      <div className="flex items-center gap-3">
        <Button onClick={() => save.mutate({
          female: draft.female,
          male: draft.male,
          okuro_gender: draft.okuro_gender,
          speed: draft.speed,
          fx_enabled: draft.fx_enabled,
        })} disabled={save.isPending || !dirty}>
          {save.isPending ? (
            <Loader2 size={14} className="mr-1.5 animate-spin" />
          ) : (
            <Save size={14} className="mr-1.5" />
          )}
          Save voices
        </Button>
        <span className="text-2xs text-fg-tertiary">
          {dirty ? "Unsaved changes." : "Applies to your next morning brief / podcast."}
        </span>
      </div>
    </div>
  );
}
