import { useEffect, useMemo, useState } from "react";
import { Cpu, Loader2, Monitor, MemoryStick } from "lucide-react";
import { embedApi, type EmbedDetection, type EmbedTierOptions } from "@/lib/api";
import { OnboardingPage } from "../page";

/**
 * Onboarding step — embedding tier picker.
 *
 * Detects host hardware, recommends a tier (LOW for laptop-safe, HIGH for
 * a non-display GPU with ≥8 GB VRAM), and lets the user override. The
 * device dropdown only appears when HIGH is chosen; the display GPU is
 * disabled by default in that picker.
 *
 * On Next, the persisted ~/.okuro/embed-config.yaml is written via
 * PUT /api/embed/config (chosen_by="onboarding"). install_all_services
 * later in the wizard reads it and produces the right unit Environment
 * lines.
 */

type Choice = "recommended" | "low" | "high";

export function EmbedSetupStep() {
  const [detection, setDetection] = useState<EmbedDetection | null>(null);
  const [tiers, setTiers] = useState<EmbedTierOptions | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [choice, setChoice] = useState<Choice>("recommended");
  const [deviceOverride, setDeviceOverride] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void Promise.all([embedApi.detect(), embedApi.tiers()])
      .then(([det, tiersResp]) => {
        setDetection(det);
        setTiers(tiersResp);
      })
      .catch((e) =>
        setErr(e instanceof Error ? e.message : "Hardware detect failed"),
      )
      .finally(() => setLoading(false));
  }, []);

  const resolvedTier: "low" | "high" = useMemo(() => {
    if (choice === "recommended") return tiers?.recommended ?? "low";
    return choice;
  }, [choice, tiers]);

  const resolvedDevice = useMemo(() => {
    if (choice === "recommended") return tiers?.recommended_device ?? "cpu";
    if (resolvedTier === "low") return "cpu";
    if (deviceOverride) return deviceOverride;
    return tiers?.recommended_device ?? "cpu";
  }, [choice, resolvedTier, deviceOverride, tiers]);

  const save = async () => {
    if (!tiers) throw new Error("Tier list not loaded");
    setSaving(true);
    try {
      await embedApi.putConfig({
        tier: resolvedTier,
        device: resolvedDevice,
        chosen_by: "onboarding",
      });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <OnboardingPage
        question="Detecting hardware…"
        canAdvance={false}
      >
        <div className="flex items-center gap-3 text-sm text-fg-tertiary">
          <Loader2 size={14} className="animate-spin" />
          Scanning GPUs, CPU and memory.
        </div>
      </OnboardingPage>
    );
  }

  if (err || !detection || !tiers) {
    return (
      <OnboardingPage
        question="Hardware detection failed"
        subtitle={err ?? "Try refreshing or pick a tier manually."}
        canAdvance={true}
        onAdvance={async () => {
          await embedApi.putConfig({
            tier: "low",
            device: "cpu",
            chosen_by: "onboarding",
          });
        }}
      >
        <div className="text-xs text-fg-tertiary">
          Falling back to LOW + CPU. You can change this later under Settings.
        </div>
      </OnboardingPage>
    );
  }

  const recommendedSpec = tiers.tiers.find((t) => t.tier === tiers.recommended);

  return (
    <OnboardingPage
      question="Embedding model"
      subtitle="Pick the tier that fits your hardware. You can change this later in Settings."
      canAdvance={!saving}
      onAdvance={save}
    >
      <div className="flex flex-col gap-6">
        {/* Detection card */}
        <div className="rounded border border-border bg-surface-elevated p-4 text-sm">
          <div className="text-xs uppercase tracking-wider text-fg-tertiary mb-3">
            Detected hardware
          </div>
          <div className="space-y-2">
            {detection.gpus.length === 0 ? (
              <div className="text-fg-secondary">
                No NVIDIA GPU detected — embedding will run on CPU.
              </div>
            ) : (
              detection.gpus.map((g) => (
                <div key={g.index} className="flex items-center gap-3">
                  <Monitor size={14} className="text-fg-tertiary" />
                  <span className="font-mono text-xs text-fg-tertiary">
                    cuda:{g.index}
                  </span>
                  <span className="text-fg-primary">{g.name}</span>
                  <span className="text-fg-tertiary">{g.vram_gb} GB</span>
                  {g.is_display && (
                    <span className="rounded bg-warning-subtle/15 text-warning text-[11px] px-1.5 py-0.5">
                      display GPU
                    </span>
                  )}
                </div>
              ))
            )}
            <div className="flex items-center gap-3 text-fg-tertiary text-xs pt-1">
              <Cpu size={12} />
              {detection.cpu_cores} cores
              <MemoryStick size={12} className="ml-2" />
              {detection.ram_gb} GB RAM
            </div>
          </div>
        </div>

        {/* Recommendation card */}
        <div className="rounded border border-accent/40 bg-accent-subtle/40 p-3 text-sm text-fg-primary">
          <div className="text-xs uppercase tracking-wider text-fg-tertiary mb-1">
            Recommended
          </div>
          <div className="flex items-baseline gap-2">
            <span className="font-bold uppercase">{tiers.recommended}</span>
            {recommendedSpec && (
              <span className="text-fg-tertiary text-xs">
                — {recommendedSpec.description}
              </span>
            )}
          </div>
          <div className="text-xs text-fg-tertiary mt-1">
            Device: <span className="font-mono">{tiers.recommended_device}</span>
            {detection.display_gpu_index !== null && (
              <span className="ml-2">
                (display GPU cuda:{detection.display_gpu_index} excluded)
              </span>
            )}
          </div>
        </div>

        {/* Tier picker */}
        <div className="flex flex-col gap-2">
          <label className="flex items-start gap-3 cursor-pointer rounded border border-border p-3 hover:bg-surface-elevated">
            <input
              type="radio"
              name="embed-tier"
              checked={choice === "recommended"}
              onChange={() => setChoice("recommended")}
              className="mt-1"
            />
            <div>
              <div className="text-sm text-fg-primary">Recommended (auto)</div>
              <div className="text-xs text-fg-tertiary">
                {tiers.recommended.toUpperCase()} on{" "}
                <span className="font-mono">{tiers.recommended_device}</span>
              </div>
            </div>
          </label>

          {tiers.tiers.map((t) => (
            <label
              key={t.tier}
              className="flex items-start gap-3 cursor-pointer rounded border border-border p-3 hover:bg-surface-elevated"
            >
              <input
                type="radio"
                name="embed-tier"
                checked={choice === t.tier}
                onChange={() => setChoice(t.tier)}
                className="mt-1"
              />
              <div className="flex-1">
                <div className="text-sm text-fg-primary uppercase">
                  {t.tier}
                  <span className="ml-2 normal-case text-xs text-fg-tertiary">
                    {t.model_id} · {t.dim}d
                  </span>
                </div>
                <div className="text-xs text-fg-tertiary">{t.description}</div>
              </div>
            </label>
          ))}
        </div>

        {/* Device picker — only when HIGH chosen */}
        {choice === "high" && detection.gpus.length > 0 && (
          <div className="flex flex-col gap-2">
            <label className="text-xs uppercase tracking-wider text-fg-tertiary">
              GPU device
            </label>
            <select
              value={deviceOverride ?? tiers.recommended_device}
              onChange={(e) => setDeviceOverride(e.target.value)}
              className="rounded border border-border bg-surface-elevated text-sm text-fg-primary px-3 py-2"
            >
              {detection.gpus.map((g) => (
                <option
                  key={g.index}
                  value={`cuda:${g.index}`}
                  // Display GPU is selectable (override), but not the default
                  disabled={false}
                >
                  cuda:{g.index} · {g.name} · {g.vram_gb} GB
                  {g.is_display ? " [display]" : " [available]"}
                </option>
              ))}
              <option value="cpu">cpu (fallback, slow)</option>
            </select>
            {deviceOverride?.startsWith("cuda:") &&
              detection.gpus.find(
                (g) =>
                  `cuda:${g.index}` === deviceOverride && g.is_display,
              ) && (
                <div className="text-xs text-warning">
                  Pinning to the display GPU can cause browser/desktop
                  contention. Choose a non-display GPU when available.
                </div>
              )}
          </div>
        )}
      </div>
    </OnboardingPage>
  );
}
