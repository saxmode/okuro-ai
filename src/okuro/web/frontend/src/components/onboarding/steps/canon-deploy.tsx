import { useEffect, useState } from "react";
import { Check, Loader2, Shield, Wrench } from "lucide-react";
import { onboardingApi } from "@/lib/api";
import { OnboardingPage } from "../page";

/**
 * Step after cli_setup — register okuro with every detected AI CLI / IDE so
 * their agents know how to call bootstrap() and use okuro's MCP server.
 *
 * This is the step that makes okuro *visible* to the user's existing agents.
 * Without it, okuro is installed but every Claude/Cursor/Codex session stays
 * blind to the memory, roles, and cortex the user just set up.
 *
 * We ask explicit consent because it writes to the user's home directory:
 *   - Appends okuro to each CLI's MCP server registry (config files)
 *   - Generates / updates CLAUDE.md, AGENTS.md and hook files for each provider
 *   - Writes ~/.okuro/TOOL-PROTOCOL.md (shared, provider-independent)
 */
export function CanonDeployStep({ onAfterDeploy }: { onAfterDeploy?: () => void }) {
  const [targets, setTargets] = useState<Array<{ id: string; name: string; detected: boolean }> | null>(null);
  const [mcpServers, setMcpServers] = useState<string[]>([]);
  const [deploying, setDeploying] = useState(false);
  const [results, setResults] = useState<Record<string, unknown> | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    onboardingApi
      .canonTargets()
      .then((r) => {
        setTargets(r.targets);
        setMcpServers(r.mcp_servers);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "Load failed"));
  }, []);

  const detectedNames = (targets ?? []).filter((t) => t.detected).map((t) => t.name);

  const deploy = async () => {
    setDeploying(true); setErr(null);
    try {
      const r = await onboardingApi.canonDeploy();
      setResults(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Deploy failed");
    } finally { setDeploying(false); }
  };

  const done = results !== null && !Object.prototype.hasOwnProperty.call(results, "_mcp_error");

  return (
    <OnboardingPage
      question="Teach your AI tools about okuro"
      subtitle="Register okuro with each CLI so they know to call bootstrap and share memory."
      canAdvance={done}
      onSkip={onAfterDeploy}
      moreInfo={
        <div className="space-y-2">
          <p>
            Okuro is an MCP server. Until an AI CLI is told about it, that CLI
            has no way to call <code>bootstrap()</code>, read your memory, or
            search cortex. This step writes the small config + instruction
            files each CLI looks for.
          </p>
          <p>
            What gets written:
          </p>
          <ul className="list-disc pl-5 space-y-1">
            <li>
              MCP registry entry in each CLI's config file (Claude Code, Cursor,
              Codex, Gemini — one line per CLI)
            </li>
            <li>
              <code>CLAUDE.md</code>, <code>AGENTS.md</code>, or equivalent
              — short instruction files telling the agent to call{" "}
              <code>bootstrap()</code> first
            </li>
            <li>
              <code>~/.okuro/TOOL-PROTOCOL.md</code> — shared tool-usage guide
              every adapter reads
            </li>
          </ul>
          <p>
            Everything stays local. You can revert by deleting the files (or
            running <code>okuro canon uninstall</code>).
          </p>
        </div>
      }
    >
      {err && (
        <div className="mb-3 rounded border border-error/40 bg-surface-elevated text-error text-xs px-3 py-2">
          {err}
        </div>
      )}

      {targets === null ? (
        <div className="flex items-center gap-2 text-sm text-fg-tertiary">
          <Loader2 size={14} className="animate-spin" /> Scanning your tools…
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="rounded border border-border-subtle bg-surface-elevated px-4 py-3">
            <div className="flex items-center gap-2 text-xs uppercase tracking-widest text-fg-tertiary mb-2">
              <Shield size={14} />
              Detected AI tools
            </div>
            {detectedNames.length === 0 ? (
              <p className="text-sm text-fg-tertiary">
                No AI CLIs detected yet. Go back to step 1 and install at least one.
              </p>
            ) : (
              <ul className="flex flex-wrap gap-2">
                {targets.map((t) => (
                  <li
                    key={t.id}
                    className={
                      t.detected
                        ? "inline-flex items-center gap-1.5 rounded-full bg-accent-subtle text-accent px-3 py-1 text-xs"
                        : "inline-flex items-center gap-1.5 rounded-full bg-surface-subtle text-fg-disabled px-3 py-1 text-xs"
                    }
                  >
                    {t.detected && <Check size={12} />}
                    {t.name}
                    {!t.detected && " — not installed"}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="rounded border border-border-subtle bg-surface-elevated px-4 py-3">
            <div className="flex items-center gap-2 text-xs uppercase tracking-widest text-fg-tertiary mb-2">
              <Wrench size={14} />
              MCP servers to register
            </div>
            <ul className="flex flex-wrap gap-2">
              {mcpServers.map((s) => (
                <li key={s} className="inline-flex items-center gap-1 rounded-full bg-surface px-3 py-1 text-xs text-fg-primary font-mono">
                  {s}
                </li>
              ))}
            </ul>
          </div>

          {!done && (
            <button
              type="button"
              onClick={deploy}
              disabled={deploying || detectedNames.length === 0}
              className="self-start flex items-center gap-2 rounded bg-accent px-4 py-2 text-sm text-fg-inverse hover:bg-accent-hover disabled:opacity-50 transition-colors"
            >
              {deploying ? <Loader2 size={14} className="animate-spin" /> : <Shield size={14} />}
              Register okuro with {detectedNames.length} tool{detectedNames.length !== 1 && "s"}
            </button>
          )}

          {done && results && (
            <div className="rounded border border-accent/40 bg-accent-subtle px-4 py-3">
              <div className="flex items-center gap-2 text-xs text-accent mb-2">
                <Check size={14} />
                Registered. Your AI tools can now reach okuro.
              </div>
              <ul className="text-2xs text-fg-tertiary space-y-1">
                {Object.entries(results).map(([k, v]) => (
                  <li key={k} className="font-mono">
                    <span className="text-fg-primary">{k}</span>: {JSON.stringify(v)}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </OnboardingPage>
  );
}
