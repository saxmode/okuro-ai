import type { ReactNode } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { ArrowLeft, Loader2, Check, Lock, Cpu, AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import {
  sttApi,
  type SttTier,
  type SttTiersResponse,
  type SttTierOption,
} from "@/lib/api";

/**
 * /settings/stt — pick the dictation (speech-to-text) tier.
 *
 * Tiers (air / plus / pro) and their descriptions come from the backend
 * (voice.tier.TIERS) — single source of truth. Selecting a tier PUTs
 * /api/voice/stt/tier, which persists ~/.okuro/stt-config.yaml and takes
 * effect on the next dictation (no restart). Tiers above the subscription
 * entitlement are locked; OKURO_STT_TIER forces a tier for testing and, when
 * set, overrides this page (surfaced as a banner).
 */
export function SettingsSttPage() {
  const qc = useQueryClient();
  const info = useQuery({ queryKey: ["stt", "tiers"], queryFn: sttApi.tiers });

  const setTier = useMutation({
    mutationFn: sttApi.setTier,
    onSuccess: (res) => {
      toast.success(`Dictation tier: ${res.effective.toUpperCase()}`, {
        description: res.stream_backend,
      });
      void qc.invalidateQueries({ queryKey: ["stt"] });
    },
    onError: (e: unknown) => toast.error(String((e as Error)?.message ?? e)),
  });

  if (info.isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-fg-tertiary text-sm">
        <Loader2 size={14} className="animate-spin mr-2" />
        Loading dictation settings…
      </div>
    );
  }
  if (info.error || !info.data) {
    return (
      <div className="p-6 text-sm text-error">
        Failed to load dictation settings.
      </div>
    );
  }

  const data = info.data;

  return (
    <div className="flex flex-col gap-6 p-6">
      <PageHeader
        title="Dictation"
        subtitle="Choose the speech-to-text model okuro uses when you record notes. Higher tiers are more accurate; the top tier needs a GPU."
        right={
          <Link
            to="/settings"
            className="flex items-center gap-1 text-xs text-fg-tertiary hover:text-fg-primary"
          >
            <ArrowLeft size={12} />
            Back to Settings
          </Link>
        }
      />

      {data.reason === "env-override" && (
        <div className="rounded border border-warning/40 bg-warning-subtle/10 p-3 flex items-start gap-2 text-sm">
          <AlertTriangle size={14} className="text-warning mt-0.5 shrink-0" />
          <div>
            <span className="text-fg-primary font-medium">
              Tier is forced by <code>OKURO_STT_TIER</code>.
            </span>{" "}
            <span className="text-fg-secondary">
              Dictation uses <b>{data.effective.toUpperCase()}</b> regardless of
              the choice below. Unset the env var to control it from here.
            </span>
          </div>
        </div>
      )}

      <EffectiveChip data={data} />

      <div className="flex flex-col gap-3">
        <div className="text-xs uppercase tracking-wider text-fg-tertiary">
          Tiers
        </div>
        {data.tiers.map((t) => (
          <TierCard
            key={t.tier}
            spec={t}
            data={data}
            busy={setTier.isPending}
            onSelect={() => setTier.mutate(t.tier)}
          />
        ))}
      </div>
    </div>
  );
}

function EffectiveChip({ data }: { data: SttTiersResponse }) {
  const reasonLabel =
    data.reason === "auto"
      ? "automatic (best your hardware allows)"
      : data.reason === "settings"
        ? "your choice"
        : "forced by env";
  return (
    <div className="rounded border border-border bg-surface-elevated p-4 flex flex-wrap items-center gap-x-6 gap-y-3 text-sm">
      <Field label="Active tier">
        <span className="font-mono text-fg-primary uppercase">{data.effective}</span>
        <span className="text-fg-tertiary"> · {reasonLabel}</span>
      </Field>
      <div className="border-l border-border h-10 hidden sm:block" />
      <Field label="Streaming">
        <span className="font-mono text-fg-secondary">{data.stream_backend}</span>
      </Field>
      <div className="border-l border-border h-10 hidden sm:block" />
      <Field label="This machine">
        <span className="font-mono text-fg-secondary">
          runs up to {data.recommended.toUpperCase()} · {data.recommended_device}
        </span>
      </Field>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wider text-fg-tertiary">{label}</div>
      <div>{children}</div>
    </div>
  );
}

const RANK: Record<SttTier, number> = { air: 0, plus: 1, pro: 2 };

function TierCard({
  spec,
  data,
  busy,
  onSelect,
}: {
  spec: SttTierOption;
  data: SttTiersResponse;
  busy: boolean;
  onSelect: () => void;
}) {
  const isEffective = data.effective === spec.tier;
  const isSelected = data.selected === spec.tier;
  const isRecommended = data.recommended === spec.tier;
  const locked = RANK[spec.tier] > RANK[data.entitled];
  const overHardware = RANK[spec.tier] > RANK[data.recommended];

  return (
    <div
      className={`rounded border p-4 bg-surface-elevated ${
        isEffective ? "border-accent" : "border-border"
      }`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-bold text-fg-primary">{spec.label}</span>
            {isEffective && (
              <Badge className="bg-accent/15 text-accent">
                <Check size={11} /> active
              </Badge>
            )}
            {isRecommended && !isEffective && (
              <Badge className="bg-success-subtle/15 text-success">recommended</Badge>
            )}
            {spec.needs_gpu && (
              <Badge className="bg-surface-subtle text-fg-tertiary">
                <Cpu size={11} /> GPU
              </Badge>
            )}
            {locked && (
              <Badge className="bg-warning-subtle/15 text-warning">
                <Lock size={11} /> okuro-{spec.tier}
              </Badge>
            )}
          </div>
          <p className="text-xs text-fg-secondary mt-1.5">{spec.characteristics}</p>
          <p className="text-xs text-fg-tertiary mt-1.5">
            <span className="text-fg-secondary">Best for:</span> {spec.best_for}
          </p>
          <div className="text-[11px] font-mono text-fg-tertiary mt-2">
            stream {spec.stream_model} · batch {spec.batch_model}
          </div>
          {overHardware && !locked && (
            <p className="text-[11px] text-warning mt-1.5">
              Above this machine’s capability — may be slow or fail.
            </p>
          )}
        </div>
        <div className="shrink-0">
          {isEffective ? (
            <span className="text-xs text-fg-tertiary">in use</span>
          ) : locked ? (
            <span className="text-xs text-fg-tertiary">unavailable</span>
          ) : (
            <Button size="sm" disabled={busy} onClick={onSelect}>
              {isSelected ? "Re-apply" : "Use"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

function Badge({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] ${className ?? ""}`}
    >
      {children}
    </span>
  );
}
