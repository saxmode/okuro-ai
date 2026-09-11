import { useState, useRef } from "react";
import { FileText, Github, Loader2, X } from "lucide-react";
import { onboardingApi } from "@/lib/api";
import { OnboardingPage } from "../page";
import { UrlList } from "../inputs/url-list";

type ExtractResult = Record<string, unknown> & { _source?: string; _source_name?: string };

/**
 * Profile import — LinkedIn PDF + GitHub handle + freeform URLs, all get
 * extracted separately then merged. Optional: user can Skip straight past.
 * On save, merged data is patched into the profile and `profile_sources.imported`
 * is set so downstream steps and settings know this step ran.
 */
export function ProfileSourcesStep({ onAfterMerge }: { onAfterMerge?: () => void }) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [githubHandle, setGithubHandle] = useState("");
  const [urls, setUrls] = useState<string[]>([""]);
  const [extracts, setExtracts] = useState<ExtractResult[]>([]);
  const [loading, setLoading] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const addExtract = (r: ExtractResult) => setExtracts((prev) => [...prev, r]);

  const handleLinkedInFile = async (f: File) => {
    setLoading("linkedin"); setErr(null);
    try {
      const r = await onboardingApi.extractLinkedIn(f);
      addExtract(r as ExtractResult);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "LinkedIn import failed");
    } finally { setLoading(null); }
  };

  const handleGitHub = async () => {
    if (!githubHandle.trim()) return;
    setLoading("github"); setErr(null);
    try {
      const r = await onboardingApi.extractGitHub(githubHandle.trim());
      addExtract(r as ExtractResult);
      setGithubHandle("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "GitHub import failed");
    } finally { setLoading(null); }
  };

  const handleUrls = async () => {
    const list = urls.map((u) => u.trim()).filter(Boolean);
    if (!list.length) return;
    setLoading("urls"); setErr(null);
    try {
      for (const u of list) {
        const r = await onboardingApi.extractUrl(u);
        addExtract(r as ExtractResult);
      }
      setUrls([""]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "URL import failed");
    } finally { setLoading(null); }
  };

  const mergeAndSave = async () => {
    if (!extracts.length) return;
    setLoading("merge"); setErr(null);
    try {
      const merged = await onboardingApi.mergeSources(extracts);
      const patch: Record<string, unknown> = {};
      for (const k of ["expertise", "communication", "work_style", "identity"]) {
        if (merged[k]) patch[k] = merged[k];
      }

      // profession → identity.profession (LLM extracts this as top-level string)
      const profession = merged["profession"];
      if (typeof profession === "string" && profession.trim()) {
        const id = (patch["identity"] as Record<string, unknown> | undefined) ?? {};
        patch["identity"] = { ...id, profession: profession.trim() };
      }

      // interests → work_style.interests (list, deduped). LLM returns a list of
      // topical interests; behavioral contract + settings read work_style.interests.
      const interests = merged["interests"];
      if (Array.isArray(interests) && interests.length) {
        const ws = (patch["work_style"] as Record<string, unknown> | undefined) ?? {};
        const existing = Array.isArray(ws["interests"]) ? (ws["interests"] as unknown[]) : [];
        const dedup = Array.from(
          new Set(
            [...existing, ...interests]
              .filter((v): v is string => typeof v === "string" && v.trim().length > 0)
              .map((v) => v.trim())
          )
        );
        patch["work_style"] = { ...ws, interests: dedup };
      }

      patch["profile_sources"] = {
        imported: true,
        sources: extracts.map((e) => e._source_name || e._source || "unknown"),
      };
      await onboardingApi.patchProfile(patch);
      onAfterMerge?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Merge failed");
    } finally { setLoading(null); }
  };

  return (
    <OnboardingPage
      question="Import your profile"
      subtitle="Skip this if you'd rather fill things in by hand — it just saves typing."
      canAdvance={extracts.length === 0 || !!extracts.length /* always ok; merge is manual */}
      onSkip={onAfterMerge}
    >
      {err && (
        <div className="mb-3 rounded border border-error/40 bg-surface-elevated text-error text-xs px-3 py-2">
          {err}
        </div>
      )}

      <div className="flex flex-col gap-6">
        {/* LinkedIn PDF */}
        <div className="flex flex-col gap-2">
          <span className="text-xs uppercase tracking-widest text-fg-tertiary">LinkedIn (PDF export)</span>
          <input
            ref={fileInputRef}
            type="file"
            accept="application/pdf"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleLinkedInFile(f);
            }}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={loading === "linkedin"}
            className="flex items-center gap-2 self-start rounded border border-border-subtle bg-surface-elevated px-3 py-2 text-sm text-fg-primary hover:border-accent/50 hover:text-accent disabled:opacity-50 transition-colors"
          >
            {loading === "linkedin" ? <Loader2 size={14} className="animate-spin" /> : <FileText size={14} />}
            Upload PDF
          </button>
        </div>

        {/* GitHub */}
        <div className="flex flex-col gap-2">
          <span className="text-xs uppercase tracking-widest text-fg-tertiary">GitHub handle</span>
          <div className="flex items-center gap-2">
            <div className="flex items-center flex-1 border-b border-border-subtle focus-within:border-accent">
              <Github size={14} className="text-fg-tertiary mr-2" />
              <input
                type="text"
                value={githubHandle}
                onChange={(e) => setGithubHandle(e.target.value)}
                placeholder="octocat"
                className="flex-1 bg-transparent border-0 focus:outline-none text-base text-fg-primary placeholder:text-fg-disabled py-2"
              />
            </div>
            <button
              type="button"
              onClick={handleGitHub}
              disabled={!githubHandle.trim() || loading === "github"}
              className="rounded border border-accent/40 px-3 py-1.5 text-xs text-accent disabled:opacity-30 hover:bg-accent-subtle transition-colors"
            >
              {loading === "github" ? <Loader2 size={12} className="animate-spin" /> : "Import"}
            </button>
          </div>
        </div>

        {/* URLs */}
        <div className="flex flex-col gap-2">
          <span className="text-xs uppercase tracking-widest text-fg-tertiary">Other URLs (personal site, etc.)</span>
          <UrlList urls={urls} onChange={setUrls} />
          <button
            type="button"
            onClick={handleUrls}
            disabled={!urls.some((u) => u.trim()) || loading === "urls"}
            className="self-start rounded border border-accent/40 px-3 py-1.5 text-xs text-accent disabled:opacity-30 hover:bg-accent-subtle transition-colors"
          >
            {loading === "urls" ? <Loader2 size={12} className="animate-spin" /> : "Import URLs"}
          </button>
        </div>

        {/* Staged extracts + merge */}
        {extracts.length > 0 && (
          <div className="border-t border-border-subtle pt-4 flex flex-col gap-2">
            <span className="text-xs uppercase tracking-widest text-fg-tertiary">
              Staged imports ({extracts.length})
            </span>
            <div className="flex flex-wrap gap-2">
              {extracts.map((e, i) => (
                <span key={i} className="inline-flex items-center gap-1 text-xs bg-accent-subtle text-accent rounded-full px-3 py-1">
                  {String(e._source_name ?? e._source ?? "source")}
                  <button
                    type="button"
                    onClick={() => setExtracts((prev) => prev.filter((_, idx) => idx !== i))}
                    aria-label="Remove"
                    className="hover:bg-accent/20 rounded-full p-0.5"
                  >
                    <X size={10} />
                  </button>
                </span>
              ))}
            </div>
            <button
              type="button"
              onClick={mergeAndSave}
              disabled={loading === "merge"}
              className="mt-2 self-start rounded bg-accent px-4 py-2 text-sm text-fg-inverse hover:bg-accent-hover disabled:opacity-50 transition-colors"
            >
              {loading === "merge" ? <Loader2 size={14} className="animate-spin" /> : "Merge & save"}
            </button>
          </div>
        )}
      </div>
    </OnboardingPage>
  );
}
