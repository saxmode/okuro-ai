import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { onboardingApi } from "@/lib/api";
import type { InferenceCli } from "@/types/api";
import { OnboardingPage } from "../page";
import {
  ActionCard,
  type ActionCardStatus,
} from "../inputs/action-card";

/**
 * Step 1 — Connect at least one inference CLI.
 *
 * Reads the canon-backed list of CLIs, shows an install button per CLI that
 * spawns a terminal via the backend, polls state every 3s while an action is
 * pending, and unlocks Next only once at least one CLI is authenticated.
 */
export function CliSetupStep({ onStateChange }: { onStateChange?: () => void }) {
  const [clis, setClis] = useState<InferenceCli[] | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [pending, setPending] = useState<Record<string, { kind: "install" | "auth"; terminal?: string; command?: string; fallback?: boolean }>>({});
  const [err, setErr] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const list = await onboardingApi.listClis();
      setClis(list);
      return list;
    } catch (e) {
      setErr(e instanceof Error ? e.message : "CLI list failed");
      return null;
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Continuous 5s poll of the full CLI list while this step is mounted.
  // Catches auth done in a terminal okuro didn't spawn — user runs
  // `claude login` in their own iTerm, comes back, row flips to green
  // within 5s without a manual refresh. The tighter 3s per-row poll
  // below still runs for rows with an active okuro-spawned pending
  // action; this slower loop covers the "already-installed-just-need-to-
  // log-in" case that previously stalled indefinitely at "pending
  // authentication".
  useEffect(() => {
    const handle = setInterval(() => {
      void load();
    }, 5000);
    return () => clearInterval(handle);
  }, [load]);

  // While any row has a pending action (install / sign-in okuro
  // spawned), poll ITS specific state every 3s so that row updates the
  // moment the user finishes the flow in the spawned terminal.
  useEffect(() => {
    if (Object.keys(pending).length === 0) return;
    const handle = setInterval(async () => {
      for (const toolId of Object.keys(pending)) {
        try {
          const s = await onboardingApi.cliState(toolId);
          setClis((prev) =>
            prev?.map((c) =>
              c.tool_id === toolId
                ? {
                    ...c,
                    installed: s.installed,
                    path: s.path ?? null,
                    authenticated: s.authenticated,
                  }
                : c,
            ) ?? prev,
          );
          const p = pending[toolId];
          if (!p) continue;
          const achieved =
            (p.kind === "install" && s.installed) ||
            (p.kind === "auth" && !!s.authenticated);
          if (achieved) {
            setPending((prev) => {
              const { [toolId]: _gone, ...rest } = prev;
              return rest;
            });
            onStateChange?.();
          }
        } catch {}
      }
    }, 3000);
    return () => clearInterval(handle);
  }, [pending, onStateChange]);

  const manualRefresh = async () => {
    setRefreshing(true);
    setErr(null);
    try {
      await load();
    } finally {
      setRefreshing(false);
    }
  };

  const run = async (cli: InferenceCli, kind: "install" | "auth") => {
    setErr(null);
    setBusy((b) => ({ ...b, [cli.tool_id]: true }));
    try {
      const res =
        kind === "install"
          ? await onboardingApi.installCli(cli.tool_id)
          : await onboardingApi.authenticateCli(cli.tool_id);
      if (res.status === "spawned") {
        setPending((p) => ({
          ...p,
          [cli.tool_id]: { kind, terminal: res.terminal, command: res.command },
        }));
      } else if (res.status === "no_terminal") {
        setPending((p) => ({
          ...p,
          [cli.tool_id]: { kind, command: res.command, fallback: true },
        }));
      } else if (res.status === "missing_prereq") {
        // Actionable, not silent — show the specific missing tool + a hint.
        const hint =
          res.message ??
          `This install needs ${res.prereq ?? "a missing prerequisite"}.`;
        const cmd = res.alt_command_hint ?? res.install_command_hint;
        setErr(cmd ? `${hint}\n\nThen run: ${cmd}` : hint);
      } else {
        setErr(`${kind} failed: ${res.status}`);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy((b) => ({ ...b, [cli.tool_id]: false }));
    }
  };

  const list = clis ?? [];
  const anyAuth = list.some((c) => c.installed && c.authenticated);

  return (
    <OnboardingPage
      question="Connect an AI assistant"
      subtitle="Okuro runs on your existing CLI — Claude Code, Codex, or Gemini. Install one to continue."
      canAdvance={anyAuth}
      moreInfo={
        <div className="space-y-2">
          <p>
            Okuro never talks to an LLM directly. It orchestrates one or more
            CLI tools you already use (or install now), keeping your
            authentication and billing fully under your own account.
          </p>
          <p>
            Claude Code is recommended for the broadest tool use, but any of
            the three will work — and you can wire up more later from Settings.
          </p>
        </div>
      }
    >
      {err && (
        <div className="mb-3 rounded border border-error/40 bg-surface-elevated text-error text-xs px-3 py-2 whitespace-pre-wrap">
          {err}
        </div>
      )}

      <div className="mb-3 flex items-center justify-end">
        <button
          type="button"
          onClick={manualRefresh}
          disabled={refreshing}
          className="inline-flex items-center gap-1.5 text-xs text-fg-tertiary hover:text-fg-primary disabled:opacity-40 transition-colors"
          aria-label="Refresh CLI status"
          title="Refresh CLI status"
        >
          {refreshing ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <RefreshCw size={12} />
          )}
          Refresh
        </button>
      </div>

      <div className="flex flex-col gap-3">
        {list.map((cli) => {
          const status: ActionCardStatus = cli.authenticated
            ? "authenticated"
            : cli.installed
              ? "needs_auth"
              : "not_installed";
          const p = pending[cli.tool_id];
          const pendingMessage = p && !p.fallback
            ? `Opened in ${p.terminal ?? "your terminal"}. Finish there, then this row updates automatically.`
            : undefined;
          const fallbackCmd = p && p.fallback ? p.command : undefined;
          return (
            <ActionCard
              key={cli.tool_id}
              title={cli.name}
              description={cli.description}
              recommended={cli.recommended}
              status={status}
              busy={busy[cli.tool_id]}
              primaryLabel={cli.installed ? "Sign in" : "Install"}
              onPrimary={() => run(cli, cli.installed ? "auth" : "install")}
              pendingMessage={pendingMessage}
              pendingFallbackCommand={fallbackCmd}
            />
          );
        })}
      </div>
    </OnboardingPage>
  );
}
