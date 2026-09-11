import { useEffect, useState } from "react";
import { Loader2, Sparkles } from "lucide-react";
import { onboardingApi } from "@/lib/api";
import type { DesignOption } from "@/types/api";
import { OnboardingPage } from "../page";
import { PickGrid, type PickItem } from "../inputs/pick-grid";
import { UrlList } from "../inputs/url-list";

/**
 * Design step — pick a baseline profile, or paste URLs to scrape a fresh
 * profile from. Single-select; selected profile id is patched into the
 * profile's design section.
 */
export function DesignStep({ onAfterSave }: { onAfterSave?: () => void }) {
  const [options, setOptions] = useState<DesignOption[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [urls, setUrls] = useState<string[]>([""]);
  const [customName, setCustomName] = useState("");
  const [loading, setLoading] = useState(true);
  const [extracting, setExtracting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    onboardingApi
      .designOptions()
      .then((opts) => {
        setOptions(opts);
        if (opts.length && !selected.length) setSelected([opts[0]!.id]);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "Load failed"))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const extractFromUrls = async () => {
    const list = urls.map((u) => u.trim()).filter(Boolean);
    if (!list.length) return;
    setExtracting(true); setErr(null);
    try {
      const newOpt = await onboardingApi.extractDesign(list, customName.trim() || undefined);
      const refreshed = await onboardingApi.designOptions();
      setOptions(refreshed);
      setSelected([newOpt.id]);
      setUrls([""]); setCustomName("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Extract failed");
    } finally { setExtracting(false); }
  };

  const save = async () => {
    if (!selected.length) return;
    setSaving(true); setErr(null);
    try {
      // `design.kit` NAMES THE ACTIVE DESIGN SYSTEM. It used to write
      // `design.profile`, which already meant a v1 design profile elsewhere —
      // one field answering two questions. The engine still reads `profile` as
      // a fallback so an install made before this keeps its choice.
      await onboardingApi.patchProfile({ design: { kit: selected[0] } });
      onAfterSave?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
      setSaving(false);
    }
  };

  const grid: PickItem[] = options.map((o) => ({
    value: o.id,
    label: o.name,
    description: o.description,
    hint: o.source === "scraped" ? "scraped" : undefined,
  }));

  return (
    <OnboardingPage
      question="Pick a visual identity"
      subtitle="Sets the design tokens agents use when generating UI. You can change it later."
      canAdvance={!saving && selected.length > 0}
      onSkip={onAfterSave}
      moreInfo={
        <p>
          Each profile is a token bundle: palette, typography scale, spacing,
          motion. You can also paste 1–6 URLs and okuro will scrape a visual
          identity from them — useful if you want agents to match your
          company's site.
        </p>
      }
    >
      {err && (
        <div className="mb-3 rounded border border-error/40 bg-surface-elevated text-error text-xs px-3 py-2 whitespace-pre-wrap font-mono leading-relaxed">
          {err}
        </div>
      )}

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-fg-tertiary">
          <Loader2 size={14} className="animate-spin" /> Loading profiles…
        </div>
      ) : (
        <div className="flex flex-col gap-6">
          <PickGrid items={grid} values={selected} onChange={setSelected} mode="single" />

          <div className="border-t border-border-subtle pt-4 flex flex-col gap-2">
            <span className="text-xs uppercase tracking-widest text-fg-tertiary">
              Or scrape from URLs
            </span>
            <input
              type="text"
              value={customName}
              onChange={(e) => setCustomName(e.target.value)}
              placeholder="Name this profile (optional)"
              className="bg-transparent border-0 border-b border-border-subtle focus:border-accent focus:outline-none text-sm text-fg-primary placeholder:text-fg-disabled py-2"
            />
            <UrlList urls={urls} onChange={setUrls} max={6} />
            <button
              type="button"
              onClick={extractFromUrls}
              disabled={!urls.some((u) => u.trim()) || extracting}
              className="self-start flex items-center gap-2 rounded border border-accent/40 px-3 py-1.5 text-xs text-accent disabled:opacity-30 hover:bg-accent-subtle transition-colors"
            >
              {extracting ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
              Scrape & save as new profile
            </button>
          </div>

          <button
            type="button"
            onClick={save}
            disabled={!selected.length || saving}
            className="self-start rounded bg-accent px-4 py-2 text-sm text-fg-inverse hover:bg-accent-hover disabled:opacity-50 transition-colors"
          >
            {saving ? <Loader2 size={14} className="animate-spin" /> : "Save & continue"}
          </button>
        </div>
      )}
    </OnboardingPage>
  );
}
