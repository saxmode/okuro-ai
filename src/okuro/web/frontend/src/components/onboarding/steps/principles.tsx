import { useEffect, useState } from "react";
import { Loader2, Plus } from "lucide-react";
import { onboardingApi } from "@/lib/api";
import { OnboardingPage } from "../page";
import { PickGrid, type PickItem } from "../inputs/pick-grid";

/**
 * Principles step — library grid + custom "formulate from a need" entry.
 * Backend's formulatePrinciple turns a plain-English need into a structured
 * principle; we append it to the custom list and auto-select it.
 */
export function PrinciplesStep({ onAfterSave }: { onAfterSave?: () => void }) {
  const [available, setAvailable] = useState<PickItem[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [custom, setCustom] = useState<Array<{ id: string; name: string; description: string }>>([]);
  const [customDraft, setCustomDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [formulating, setFormulating] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    onboardingApi
      .principles()
      .then((r) => {
        setAvailable(
          r.available.map((p) => ({
            value: p.id,
            label: p.name,
            description: p.description,
          })),
        );
        setSelected(r.selected);
        setCustom(r.custom);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "Load failed"))
      .finally(() => setLoading(false));
  }, []);

  const formulate = async () => {
    if (!customDraft.trim()) return;
    setFormulating(true); setErr(null);
    try {
      const p = await onboardingApi.formulatePrinciple(customDraft.trim());
      setCustom((prev) => [...prev, p]);
      setSelected((prev) => [...prev, p.id]);
      setCustomDraft("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Formulate failed");
    } finally { setFormulating(false); }
  };

  // Wire save into the shell's onAdvance so clicking Next ALWAYS persists.
  // Previously a separate "Save" button was the only persistence path; users
  // who clicked Next without it lost their selection silently.
  //
  // The save itself is the only thing that gates the advance. The
  // refreshProfile callback (onAfterSave) is fire-and-forget — if the parent's
  // profile fetch fails, the save STILL succeeded and the user should advance.
  // Coupling the two was bouncing users back to this step every time
  // refreshProfile threw, even though their selection had landed in the DB.
  const advance = async () => {
    setErr(null);
    try {
      await onboardingApi.savePrinciples(selected, custom);
    } catch (e) {
      // Show the actual error AND log it for browser-console diagnosis. Throw
      // to abort the advance — the selection didn't persist, user must retry.
      // eslint-disable-next-line no-console
      console.error("savePrinciples failed:", e);
      const status = (e as { status?: number })?.status;
      const msg = e instanceof Error ? e.message : "Save failed";
      setErr(status ? `${msg} (HTTP ${status})` : msg);
      throw e;
    }
    // Refresh parent profile cache — best-effort, never blocks the advance.
    try {
      const result = onAfterSave?.();
      if (result && typeof (result as Promise<unknown>).catch === "function") {
        (result as Promise<unknown>).catch((err) => {
          // eslint-disable-next-line no-console
          console.warn("onAfterSave (refresh) failed; advance continues:", err);
        });
      }
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn("onAfterSave threw synchronously; advance continues:", err);
    }
  };

  const grid: PickItem[] = [
    ...available,
    ...custom.map((c) => ({
      value: c.id,
      label: c.name,
      description: c.description,
      hint: "custom",
    })),
  ];

  return (
    <OnboardingPage
      question="What matters to you?"
      subtitle="Pick the principles agents should weigh when making decisions for you."
      canAdvance={selected.length > 0}
      onAdvance={advance}
      onSkip={onAfterSave}
      moreInfo={
        <p>
          Principles are the durable rules you want agents to respect — short
          phrases like "default to open-source" or "never touch production on
          Fridays". Okuro surfaces them in the agent bootstrap packet so every
          session starts with them in view.
        </p>
      }
    >
      {err && (
        <div className="mb-3 rounded border border-error/40 bg-surface-elevated text-error text-xs px-3 py-2">
          {err}
        </div>
      )}

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-fg-tertiary">
          <Loader2 size={14} className="animate-spin" /> Loading principles…
        </div>
      ) : (
        <div className="flex flex-col gap-6">
          <PickGrid items={grid} values={selected} onChange={setSelected} mode="multi" />

          <div className="border-t border-border-subtle pt-4 flex flex-col gap-2">
            <span className="text-xs uppercase tracking-widest text-fg-tertiary">
              Add your own
            </span>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={customDraft}
                onChange={(e) => setCustomDraft(e.target.value)}
                placeholder="e.g. never ship on Fridays"
                className="flex-1 bg-transparent border-0 border-b border-border-subtle focus:border-accent focus:outline-none text-base text-fg-primary placeholder:text-fg-disabled py-2"
              />
              <button
                type="button"
                onClick={formulate}
                disabled={!customDraft.trim() || formulating}
                className="rounded border border-accent/40 p-1.5 text-accent hover:bg-accent-subtle disabled:opacity-30 transition-colors"
              >
                {formulating ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}
              </button>
            </div>
            <span className="text-2xs text-fg-disabled">
              Describe a rule in plain English — okuro turns it into a formal principle.
            </span>
          </div>

          {/* persistence is handled by onAdvance — no explicit Save button */}
        </div>
      )}
    </OnboardingPage>
  );
}
