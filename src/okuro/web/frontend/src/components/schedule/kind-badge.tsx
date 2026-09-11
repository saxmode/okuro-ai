import { Badge } from "@/components/ui/badge";
import type { TaskKind, TaskTier } from "@/types/api";

// Shared by BOTH schedule panels (daemon jobs + recurring runs) so a daemon
// task and a recurring def stay directly comparable. Two panels rendering the
// same concept two ways is the thing this file exists to prevent.

const KIND_LABEL: Record<TaskKind, string> = {
  script: "script",
  llm: "LLM",
  mixed: "mixed",
  orchestrator: "orchestrator",
};

// Colour carries the cost signal: a model call is the thing worth noticing in
// a list that is mostly SQL. script is deliberately the quietest.
const KIND_CLASS: Record<TaskKind, string> = {
  script: "bg-surface text-tertiary border-border",
  llm: "bg-accent/10 text-accent border-accent/30",
  mixed: "bg-accent/5 text-fg-muted border-accent/20",
  orchestrator: "bg-primary/10 text-primary border-primary/30",
};

const KIND_HELP: Record<TaskKind, string> = {
  script: "Deterministic — no model call",
  llm: "Always calls a model",
  mixed: "Deterministic; calls a model only when there is work",
  orchestrator: "Spawns an engine, which picks its own models",
};

export function KindBadge({ kind }: { kind: TaskKind }) {
  return (
    <Badge
      variant="outline"
      className={KIND_CLASS[kind] ?? KIND_CLASS.script}
      title={KIND_HELP[kind]}
    >
      {KIND_LABEL[kind] ?? kind}
    </Badge>
  );
}

// What each tier resolves to on claude, the detected provider. Shown as a
// tooltip because "quality" alone doesn't tell you it means opus.
const TIER_HELP: Record<Exclude<TaskTier, "">, string> = {
  fast: "fast tier — haiku",
  standard: "standard tier — sonnet",
  quality: "quality tier — opus (codex: reasoning_effort=high)",
};

const TIER_CLASS: Record<Exclude<TaskTier, "">, string> = {
  fast: "bg-surface text-tertiary border-border",
  standard: "bg-surface text-fg-muted border-border",
  quality: "bg-warning/10 text-warning border-warning/30",
};

export function TierBadge({ tier }: { tier: TaskTier }) {
  // A script has no tier — render nothing rather than an empty badge.
  if (!tier) return <span className="text-tertiary">—</span>;
  return (
    <Badge variant="outline" className={TIER_CLASS[tier]} title={TIER_HELP[tier]}>
      {tier}
    </Badge>
  );
}

// The embedding model is a separate axis from the model tier — a task can hit
// it while making no LLM call at all. Only rendered when true.
export function EmbedsBadge({ embeds }: { embeds: boolean }) {
  if (!embeds) return null;
  return (
    <Badge
      variant="outline"
      className="bg-surface text-tertiary border-border"
      title="Also calls the embedding model (Qwen3-Embedding-0.6B)"
    >
      embed
    </Badge>
  );
}
