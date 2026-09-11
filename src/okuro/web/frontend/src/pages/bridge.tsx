import { useMemo, useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { Loader2, Play, RefreshCw, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { StatusBadge } from "@/components/ui/status-badge";
import { FieldLabel } from "@/components/ui/form-primitives";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import { PageHeader } from "@/components/shell/page-header";
import {
  bridgeApi,
  type ProviderStatus,
  type InvokeResponse,
} from "@/lib/bridge-api";

/**
 * /bridge — surfaces the okuro.bridge subsystem.
 *
 * Three zones:
 *  1) Provider status grid — one card per CLI (claude/cursor/codex/gemini/local)
 *  2) Capability routing table — which capability goes to which provider
 *  3) Try-it-out — send a short prompt, see which provider handled it
 *
 * Every LLM-dependent feature in okuro routes through bridge_invoke.
 * A green dot here means the CLI is installed + on PATH. Red means the
 * binary isn't resolvable — the feature will surface the error inline
 * in this page's try-it-out rather than silently breaking elsewhere.
 */

const ANY_VALUE = "__any__";

export function BridgePage() {
  const {
    data: status,
    refetch: refetchStatus,
    isLoading,
    isFetching,
  } = useQuery({
    queryKey: ["bridge", "status"],
    queryFn: () => bridgeApi.status(),
    refetchInterval: 15_000,
  });

  const { data: routing } = useQuery({
    queryKey: ["bridge", "providers"],
    queryFn: () => bridgeApi.providers(),
  });

  const { data: usage } = useQuery({
    queryKey: ["bridge", "usage", "week"],
    queryFn: () => bridgeApi.usage("week"),
  });

  return (
    <div className="page-shell space-y-10">
      <PageHeader
        title="Bridge"
        subtitle="LLM router — which provider handles each type of request."
        right={
          <Button
            variant="ghost"
            size="sm"
            onClick={() => refetchStatus()}
            disabled={isFetching}
            aria-label="Refresh bridge status"
          >
            <RefreshCw
              className={cn("h-4 w-4", isFetching && "animate-spin")}
              aria-hidden="true"
            />
          </Button>
        }
      />

      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <SectionLabel>Providers</SectionLabel>
          <span className="text-2xs text-tertiary">
            {status
              ? `${status.available}/${status.total} available`
              : isLoading
                ? "loading…"
                : "—"}
          </span>
        </div>
        {!status && isLoading ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
            {[0, 1, 2, 3].map((i) => (
              <div
                key={i}
                className="h-28 animate-pulse rounded-md border border-border-subtle bg-surface-elevated"
              />
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
            {(status?.providers ?? []).map((p) => (
              <ProviderCard
                key={p.id}
                provider={p}
                invocations={usage?.by_provider?.[p.id]?.count}
              />
            ))}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <SectionLabel>Routing</SectionLabel>
        {routing && Object.keys(routing.routing).length > 0 ? (
          <div className="overflow-hidden rounded-md border border-border-subtle">
            <table className="w-full text-sm">
              <thead className="bg-surface-subtle text-2xs uppercase tracking-wider text-tertiary">
                <tr>
                  <th className="px-3 py-2 text-left">Capability</th>
                  <th className="px-3 py-2 text-left">Provider</th>
                  <th className="px-3 py-2 text-left">Status</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(routing.routing).map(([cap, prov]) => {
                  const provider = routing.providers.find((p) => p.id === prov);
                  return (
                    <tr
                      key={cap}
                      className="border-t border-border-subtle"
                    >
                      <td className="px-3 py-2 font-medium text-fg">{cap}</td>
                      <td className="px-3 py-2 text-fg-muted">{prov}</td>
                      <td className="px-3 py-2">
                        {provider ? (
                          <StatusBadge
                            tone={provider.available ? "success" : "error"}
                            label={provider.available ? "AVAILABLE" : "UNAVAILABLE"}
                            showDot
                          />
                        ) : (
                          <span className="text-2xs text-tertiary">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No routing configured" description="Add entries to ~/.okuro/config.yaml inference.routing" />
        )}
      </section>

      <section className="space-y-3">
        <SectionLabel>Try it out</SectionLabel>
        <TryItOut providers={status?.providers ?? []} routing={routing?.routing ?? {}} />
      </section>
    </div>
  );
}

function ProviderCard({
  provider,
  invocations,
}: {
  provider: ProviderStatus;
  invocations?: number;
}) {
  const tone = provider.available ? "success" : "error";
  const modelCount = Object.keys(provider.models ?? {}).length;
  return (
    <div
      className={cn(
        "flex flex-col gap-2 rounded-md border bg-surface-elevated p-3",
        provider.available
          ? "border-border-subtle"
          : "border-error/30 bg-error/5",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-bold text-fg">{provider.id}</span>
        <StatusBadge
          tone={tone}
          label={provider.available ? "UP" : "DOWN"}
          showDot
        />
      </div>
      <div className="text-2xs uppercase tracking-wider text-tertiary">
        {provider.type}
      </div>
      <div className="text-xs text-fg-muted">
        {modelCount} model{modelCount === 1 ? "" : "s"}
        {provider.capabilities.length > 0 && (
          <>
            {" · "}
            {provider.capabilities.slice(0, 2).join(", ")}
            {provider.capabilities.length > 2 && ` +${provider.capabilities.length - 2}`}
          </>
        )}
      </div>
      <div className="text-3xs text-tertiary">
        timeout {provider.default_timeout}s
        {typeof invocations === "number" && invocations > 0 && (
          <> · {invocations} calls/7d</>
        )}
      </div>
    </div>
  );
}

function TryItOut({
  providers,
  routing,
}: {
  providers: ProviderStatus[];
  routing: Record<string, string>;
}) {
  const capabilities = useMemo(() => {
    const keys = new Set<string>(Object.keys(routing));
    // routing can be empty on first paint — always offer "default"
    keys.add("default");
    return Array.from(keys).sort();
  }, [routing]);

  const [prompt, setPrompt] = useState("say hi in 3 words");
  const [capability, setCapability] = useState<string>(ANY_VALUE);
  const [provider, setProvider] = useState<string>(ANY_VALUE);
  const [lastResult, setLastResult] = useState<InvokeResponse | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      bridgeApi.invoke({
        prompt: prompt.trim(),
        capability: capability === ANY_VALUE ? undefined : capability,
        provider: provider === ANY_VALUE ? undefined : provider,
      }),
    onSuccess: (r) => {
      setLastResult(r);
      if (!r.success) {
        toast.error(r.error || "Invoke failed", {
          description: `${r.provider}/${r.model} · ${r.latency_ms}ms`,
        });
      } else {
        toast.success(`${r.provider}/${r.model}`, {
          description: `${r.latency_ms}ms`,
        });
      }
    },
    onError: (e: Error) => {
      toast.error(e.message);
    },
  });

  const canSubmit = prompt.trim().length > 0 && !mutation.isPending;

  return (
    <div className="space-y-3 rounded-md border border-border-subtle bg-surface-elevated p-4">
      <div className="grid gap-3 md:grid-cols-[1fr_auto_auto]">
        <div>
          <FieldLabel>Prompt</FieldLabel>
          <Input
            id="bridge-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Ask the bridge anything short..."
            onKeyDown={(e) => {
              if (e.key === "Enter" && canSubmit) mutation.mutate();
            }}
          />
        </div>
        <div className="min-w-[20rem]">
          <FieldLabel>Capability</FieldLabel>
          <Select value={capability} onValueChange={setCapability}>
            <SelectTrigger id="bridge-capability">
              <SelectValue placeholder="Auto" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY_VALUE}>Auto</SelectItem>
              {capabilities.map((c) => (
                <SelectItem key={c} value={c}>
                  {c}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="min-w-[20rem]">
          <FieldLabel>Provider</FieldLabel>
          <Select value={provider} onValueChange={setProvider}>
            <SelectTrigger id="bridge-provider">
              <SelectValue placeholder="Auto" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY_VALUE}>Auto</SelectItem>
              {providers.map((p) => (
                <SelectItem key={p.id} value={p.id} disabled={!p.available}>
                  {p.id}
                  {!p.available && " (down)"}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Button onClick={() => mutation.mutate()} disabled={!canSubmit}>
          {mutation.isPending ? (
            <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <Play className="mr-1 h-4 w-4" aria-hidden="true" />
          )}
          Send
        </Button>
        <span className="text-2xs text-tertiary">
          Timeout capped at 60s. Localhost-only.
        </span>
      </div>

      {lastResult && (
        <div
          className={cn(
            "space-y-2 rounded-md border p-3",
            lastResult.success
              ? "border-success/30 bg-success/5"
              : "border-error/30 bg-error/5",
          )}
        >
          <div className="flex items-center gap-3 text-2xs uppercase tracking-wider">
            <StatusBadge
              tone={lastResult.success ? "success" : "error"}
              label={lastResult.success ? "OK" : "ERROR"}
              showDot
            />
            <Badge variant="outline" className="font-mono text-2xs">
              {lastResult.provider}/{lastResult.model || "?"}
            </Badge>
            <span className="inline-flex items-center gap-1 text-tertiary">
              <Zap className="h-3 w-3" aria-hidden="true" />
              {lastResult.latency_ms}ms
            </span>
          </div>
          {lastResult.error && (
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-surface-subtle p-2 text-xs text-error">
              {lastResult.error}
            </pre>
          )}
          {lastResult.output && (
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded bg-surface-subtle p-2 text-xs text-fg-muted">
              {lastResult.output}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
