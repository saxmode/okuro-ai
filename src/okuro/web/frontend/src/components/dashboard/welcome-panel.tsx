import { useEffect, useState } from "react";
import { Check, ChevronRight, Terminal, Sparkles, X, Wrench } from "lucide-react";
import { clsx } from "clsx";
import { Link } from "react-router";
import { onboardingApi } from "@/lib/api";
import { usePulseData } from "@/hooks/use-activity-stream";
import { useRouteWithheld } from "@/lib/features-context";
import type { InferenceCli } from "@/types/api";
import { parseApiDate } from "@/lib/format";

/**
 * Post-onboarding welcome panel shown above the dashboard when a user has
 * just completed setup. Answers four questions in one glance:
 *
 *   1. What did okuro just set up?
 *   2. How do my AI tools now use okuro?
 *   3. Where do I see okuro in action?
 *   4. What should I do next?
 *
 * Dismissible via a small × button — preference persists to
 * `profile.welcome.dismissed_at`. Re-showable from Settings → Welcome.
 */
export function WelcomePanel() {
  const [clis, setClis] = useState<InferenceCli[] | null>(null);
  const [profile, setProfile] = useState<Record<string, unknown> | null>(null);
  const [dismissing, setDismissing] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const { activity, connected } = usePulseData();

  useEffect(() => {
    Promise.all([
      onboardingApi.listClis().catch(() => []),
      onboardingApi.profile().catch(() => ({})),
    ]).then(([c, p]) => {
      setClis(c);
      setProfile(p);
    }).catch((e) => setErr(e instanceof Error ? e.message : "Load failed"));
  }, []);

  if (profile === null) return null;
  const welcome = (profile?.welcome as { dismissed_at?: string } | undefined) ?? {};
  if (welcome.dismissed_at) return null;

  // Only render for users who actually completed onboarding, and only for the
  // first week after completion. Keeps the panel out of the way for
  //   (a) anyone on a fresh install before they've walked the wizard, and
  //   (b) long-term users who've already absorbed okuro's shape.
  const onboarding = (profile.onboarding as { completed_at?: string } | undefined) ?? {};
  if (!onboarding.completed_at) return null;
  const completedMs = parseApiDate(onboarding.completed_at).getTime();
  if (Number.isNaN(completedMs)) return null;
  const SEVEN_DAYS = 7 * 24 * 60 * 60 * 1000;
  if (Date.now() - completedMs > SEVEN_DAYS) return null;

  const dismiss = async () => {
    setDismissing(true);
    try {
      const p = await onboardingApi.patchProfile({
        welcome: { dismissed_at: new Date().toISOString() },
      });
      setProfile(p);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Dismiss failed");
      setDismissing(false);
    }
  };

  const authenticatedClis = (clis ?? []).filter((c) => c.installed && c.authenticated);
  const primaryCli = authenticatedClis[0];
  const liveCount = activity?.live ?? 0;

  return (
    <div className="relative rounded-lg border border-accent/30 bg-accent-subtle/30 p-6 space-y-6">
      <button
        type="button"
        onClick={dismiss}
        disabled={dismissing}
        aria-label="Dismiss welcome"
        className="absolute right-3 top-3 rounded p-1 text-fg-tertiary hover:bg-surface-elevated hover:text-fg-primary disabled:opacity-30 transition-colors"
      >
        <X size={14} />
      </button>

      {err && (
        <div className="rounded border border-error/40 bg-surface-elevated text-error text-xs px-3 py-2">
          {err}
        </div>
      )}

      <div>
        <h2 className="text-lg font-bold text-fg-primary">Welcome to okuro</h2>
        <p className="text-sm text-fg-tertiary mt-1">
          Everything's wired. Here's what just happened and what to try next.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Section 1 — Connected */}
        <Section title="Connected" icon={Check}>
          <ul className="space-y-1.5 text-sm">
            <StatusRow
              label="okuro-orchestrator"
              ok={connected}
              detail={connected ? "running" : "offline"}
            />
            <StatusRow label="okuro-daemon" ok={connected} detail={connected ? "running" : "offline"} />
            {(clis ?? []).map((c) => (
              <StatusRow
                key={c.tool_id}
                label={c.name}
                ok={c.installed && !!c.authenticated}
                detail={
                  c.authenticated
                    ? "authenticated · MCP registered"
                    : c.installed
                      ? "installed · not signed in"
                      : "not installed"
                }
              />
            ))}
          </ul>
        </Section>

        {/* Section 2 — How your CLI uses okuro */}
        <Section title="How your CLI uses okuro" icon={Terminal}>
          {primaryCli ? (
            <div className="text-sm text-fg-tertiary space-y-2">
              <p>
                In any <span className="font-mono text-fg-primary">{primaryCli.binary}</span> session,
                the agent now has access to okuro's tools. Try:
              </p>
              <code className="block rounded bg-surface px-3 py-2 text-2xs text-fg-primary select-all break-all">
                {primaryCli.binary} "call okuro.bootstrap then summarize what you know about me"
              </code>
              <p className="text-2xs text-fg-disabled">
                The agent calls <span className="font-mono">bootstrap()</span> (your profile +
                conventions + memory), then answers with the right context.
              </p>
            </div>
          ) : (
            <p className="text-sm text-fg-tertiary">
              No CLI authenticated yet — connect one from onboarding.
            </p>
          )}
        </Section>

        {/* Section 3 — Live agent activity */}
        <Section title="Live agent activity" icon={Sparkles}>
          <div className="text-sm text-fg-tertiary space-y-2">
            <p>
              <span className="text-fg-primary font-semibold">{liveCount}</span>{" "}
              {liveCount === 1 ? "session" : "sessions"} currently active.
            </p>
            <p className="text-xs">
              When an agent calls an okuro tool, the pulse on the top-right and
              the Signals page light up. Nothing yet? Run the command above and
              watch this number change.
            </p>
            <Link
              to="/inbox"
              className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
            >
              Open Inbox
              <ChevronRight size={12} />
            </Link>
          </div>
        </Section>

        {/* Section 4 — What to try first */}
        <Section title="What to try" icon={Wrench}>
          <ul className="space-y-2 text-sm">
            <TryRow
              to="/agents"
              title="See agents in action"
              detail="Live list of every running agent session."
            />
            <TryRow
              to="/brain"
              title="Browse your memory"
              detail="Everything okuro knows about you so far."
            />
            <TryRow
              to="/cortex"
              title="Index a project"
              detail="Semantic search over any codebase you point it at."
            />
            <TryRow
              to="/settings"
              title="Fine-tune your style"
              detail="Deep cognitive / communication questionnaires and more."
            />
          </ul>
        </Section>
      </div>
    </div>
  );
}

function Section({
  title,
  icon: Icon,
  children,
}: {
  title: string;
  icon: typeof Check;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded border border-border-subtle bg-surface-elevated/60 p-4">
      <div className="mb-3 flex items-center gap-2 text-xs uppercase tracking-widest text-fg-tertiary">
        <Icon size={14} />
        {title}
      </div>
      {children}
    </div>
  );
}

function StatusRow({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return (
    <li className="flex items-center justify-between gap-3">
      <span className="flex items-center gap-2">
        <span
          className={clsx(
            "h-1.5 w-1.5 rounded-full",
            ok ? "bg-success-subtle" : "bg-fg-disabled",
          )}
        />
        <span className="text-fg-primary">{label}</span>
      </span>
      <span className="text-2xs text-fg-tertiary">{detail}</span>
    </li>
  );
}

function TryRow({ to, title, detail }: { to: string; title: string; detail: string }) {
  // Gated HERE rather than at the one call site that needed it (/agents), so
  // every row in this list — and every row added later — inherits the rule.
  // An onboarding panel that recommends a switched-off page is the worst
  // possible first impression: it is the screen shown to someone who has not
  // yet learned what okuro does.
  const withheld = useRouteWithheld(to);
  if (withheld) return null;

  return (
    <li>
      <Link
        to={to}
        className="group flex items-start gap-2 text-fg-primary hover:text-accent transition-colors"
      >
        <ChevronRight size={14} className="mt-0.5 text-fg-tertiary group-hover:text-accent shrink-0" />
        <span>
          <span className="font-medium">{title}</span>
          <span className="block text-xs text-fg-tertiary">{detail}</span>
        </span>
      </Link>
    </li>
  );
}
