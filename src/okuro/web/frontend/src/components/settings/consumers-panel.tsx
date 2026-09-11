import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw, AlertTriangle } from "lucide-react";
import { onboardingApi } from "@/lib/api";
import type { ConsumerStatus, UnmanagedRegistration } from "@/types/api";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { toast } from "@/components/ui/toast";

// Visual language mirrors settings.tsx › IntegrationsTab (StatusPill + the
// bordered `bg-surface-elevated` section card). Kept as a standalone component because
// settings.tsx is a 4.7k-line monolith.

type PillTone = "ok" | "warn" | "muted" | "bad";

function Pill({ tone, children }: { tone: PillTone; children: React.ReactNode }) {
  const cls =
    tone === "ok"
      ? "text-accent border-accent/40"
      : tone === "warn"
        ? "text-warning border-warning/40"
        : tone === "bad"
          ? "text-error border-error/40"
          : "text-tertiary border-border";
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-2xs uppercase tracking-wider border ${cls}`}
    >
      {children}
    </span>
  );
}

/** Install state → pill. Desktop apps proxy install via config presence. */
function InstallPill({ c }: { c: ConsumerStatus }) {
  if (c.installed === true) return <Pill tone="ok">installed</Pill>;
  if (c.installed === false) return <Pill tone="muted">not installed</Pill>;
  return <Pill tone="muted">install unknown</Pill>;
}

/** Auth state → pill. Only rendered when the consumer is CLI-probeable. */
function AuthPill({ c }: { c: ConsumerStatus }) {
  if (!c.auth_state) return null;
  switch (c.auth_state) {
    case "ok":
      return <Pill tone="ok">signed in</Pill>;
    case "missing":
      return <Pill tone="muted">not signed in</Pill>;
    case "expired":
      return <Pill tone="warn">session expired</Pill>;
    case "ineligible":
      // Valid creds, unentitled account (e.g. retired gemini tier) — a
      // definite negative, deliberately NOT shown as "unknown".
      return <Pill tone="warn">ineligible</Pill>;
    default:
      return <Pill tone="muted">auth {c.auth_state}</Pill>;
  }
}

/** MCP registration state → pill. */
function McpPill({ c }: { c: ConsumerStatus }) {
  if (!c.supported) return <Pill tone="muted">no okuro writer</Pill>;
  if (!c.mcp_registered) return <Pill tone="warn">not registered</Pill>;
  if (c.stale) return <Pill tone="warn">MCP stale</Pill>;
  return (
    <Pill tone="ok">MCP{c.transport ? ` · ${c.transport}` : ""}</Pill>
  );
}

function ConsumerCard({
  consumer,
  onRegister,
  registering,
}: {
  consumer: ConsumerStatus;
  onRegister: (target: string) => void;
  registering: boolean;
}) {
  const c = consumer;
  return (
    <section
      data-testid={`consumer-${c.id}`}
      data-actionable={c.actionable}
      className="rounded border border-border bg-surface-elevated p-4 space-y-3"
    >
      <header className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <h3 className="text-sm font-semibold text-fg truncate">{c.name}</h3>
          <Pill tone="muted">{c.kind}</Pill>
        </div>
        {c.actionable && (
          <Button
            size="sm"
            onClick={() => onRegister(c.provider_id)}
            disabled={registering}
          >
            {registering ? (
              <Loader2 className="animate-spin" />
            ) : (
              <RefreshCw />
            )}
            {c.stale ? "Re-deploy" : "Register"}
          </Button>
        )}
      </header>

      <div className="flex flex-wrap items-center gap-2">
        <InstallPill c={c} />
        <AuthPill c={c} />
        <McpPill c={c} />
        {c.legacy_found.length > 0 && (
          <Pill tone="warn">legacy: {c.legacy_found.join(", ")}</Pill>
        )}
      </div>

      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-2xs text-tertiary">
        <dt>config</dt>
        <dd className="min-w-0 truncate font-mono text-fg">
          {c.config_path ?? "—"}
        </dd>
        <dt>transports</dt>
        <dd>
          {c.transports.length ? c.transports.join(", ") : "—"}
          {c.preferred_transport ? ` (prefers ${c.preferred_transport})` : ""}
        </dd>
      </dl>
    </section>
  );
}

function UnmanagedWarnings({ items }: { items: UnmanagedRegistration[] }) {
  if (items.length === 0) return null;
  return (
    <div className="rounded border border-warning/30 bg-warning/5 p-3 space-y-2">
      <div className="flex items-center gap-2 text-2xs font-semibold text-warning">
        <AlertTriangle className="h-3.5 w-3.5" />
        Registrations okuro does not manage
      </div>
      <ul className="space-y-2">
        {items.map((u, i) => (
          <li key={i} className="text-2xs text-warning/90">
            <div>
              <code className="font-mono">{u.server}</code> in{" "}
              <code className="font-mono">{u.path}</code>{" "}
              <span className="text-tertiary">({u.scope})</span>
            </div>
            <div className="text-tertiary">{u.note}</div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ConsumersPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["onboarding", "consumers"],
    queryFn: onboardingApi.consumers,
    refetchInterval: 15_000,
  });

  const deploy = useMutation({
    mutationFn: (targets?: string[]) => onboardingApi.canonDeploy(targets),
    onSuccess: () => {
      toast.success("okuro surface deployed");
      qc.invalidateQueries({ queryKey: ["onboarding", "consumers"] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "deploy failed"),
  });

  if (q.isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-tertiary">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Detecting consumers…
      </div>
    );
  }
  if (q.isError) {
    return (
      <EmptyState
        title="Could not load consumers"
        description={q.error instanceof Error ? q.error.message : "unknown error"}
      />
    );
  }

  const data = q.data;
  const consumers = data?.consumers ?? [];
  const actionableCount = consumers.filter((c) => c.actionable).length;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-semibold text-fg">Tools &amp; MCP</h2>
          <p className="mt-1 text-2xs text-tertiary">
            Every detected AI consumer and whether okuro&apos;s MCP surface is
            registered with it. Installed a new tool after okuro? It shows here
            with a Register action — no need to re-run onboarding.
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => deploy.mutate(undefined)}
          disabled={deploy.isPending}
        >
          {deploy.isPending ? (
            <Loader2 className="animate-spin" />
          ) : (
            <RefreshCw />
          )}
          Register all
        </Button>
      </div>

      {actionableCount > 0 && (
        <p className="text-2xs text-warning">
          {actionableCount} consumer{actionableCount === 1 ? "" : "s"} detected
          but not registered (or stale) — register below.
        </p>
      )}

      {data && <UnmanagedWarnings items={data.unmanaged} />}

      {consumers.length === 0 ? (
        <EmptyState title="No consumers detected" />
      ) : (
        <div className="space-y-3">
          {consumers.map((c) => (
            <ConsumerCard
              key={c.id}
              consumer={c}
              registering={deploy.isPending}
              onRegister={(target) => deploy.mutate([target])}
            />
          ))}
        </div>
      )}
    </div>
  );
}
