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
//
// D1 — THE ALPHA MODIFIERS ARE GONE AND THE KIT NAMES THAT REPLACE THEM WERE
// MEASURED, NOT GUESSED. `bg-accent/10` is "10 % of a colour", a number nobody
// chose; the engine publishes a name for the tint it wanted. Each pair was
// painted over the real page ground and the PNG pixels compared (isolated
// chromium, kit `standard`, both appearances):
//
//   before              after                 dark              light
//   bg-accent/10   ->   bg-accent-subtle      33,33,33 = same   230,230,230 = same
//   bg-primary/10  ->   bg-accent-subtle      33,33,33 = same   230,230,230 = same
//   bg-accent/5    ->   bg-surface-subtle     26,26,26 = same   242->248  (+6)
//   bg-warning/10  ->   bg-warning-subtle     37,21,32 -> 55,23,46
//   border-accent/30 -> border-border         60 -> 66          180 -> 208
//   border-accent/20 -> border-border-subtle  46 -> 43          206 -> 231
//   border-warning/30 -> border-border        72,25,59 -> 66,66,66
//
// CORRECTION TO WHAT THIS COMMENT FIRST SAID, because the instrument lied and
// then the comment itself changed the answer. It read "`border-warning-subtle`
// does NOT exist as a border utility — measured: it renders currentColor",
// which was true of the BUILT SHEET and false of the kit: `@theme` declares
// `--color-warning-subtle`, so Tailwind emits every namespace for it — bg,
// text AND border — as soon as a source file names the class. A probe that
// sets a class at runtime can only see what the build already emitted, so
// "absent from the bundle" was being read as "absent from the kit".
//
// AND TAILWIND'S SOURCE SCANNER DOES NOT STRIP COMMENTS, so naming the class
// in this very comment is what made it start compiling. Re-measured after:
// `border-warning-subtle` now resolves to rgb(55,23,46) in dark.
//
// The choice below is unchanged and is now a choice rather than a constraint:
// `*-subtle` is a FILL (globals.css says so in its own words — "the fill
// behind a status border/label"), so as a 1px rule it is nearly invisible
// against the fill it sits on. The warning hue is carried by `text-warning`
// and `bg-warning-subtle`, which is the accessible pair anyway (StatusBadge's
// own rule: never colour alone).
//
// AND THE MEASUREMENT FOUND A DEFECT IT WAS NOT LOOKING FOR. `bg-primary/10`
// and `bg-accent/10` composite to the SAME PIXEL in both appearances, because
// `primary` resolves to the accent colour — so `llm` and `orchestrator` have
// always rendered the identical background, and the file's opening claim that
// "colour carries the cost signal" holds for two of four kinds, not four. The
// collision is now visible in the source instead of hidden behind two token
// names, and which colour `orchestrator` should get is an aesthetic decision:
// Question Q1 of the SYSTEM pass. `text-primary` also went to `text-accent`
// (identical pixel, and `primary` is shadcn's vocabulary, not okuro-ds's —
// ruling 966d5379).
//
// `text-tertiary` on `script` STAYS. It is the standing AA failure and the open
// kit todo c581c9b2; papering over it here would hide the count.
const KIND_CLASS: Record<TaskKind, string> = {
  script: "bg-surface text-tertiary border-border",
  llm: "bg-accent-subtle text-accent border-border",
  mixed: "bg-surface-subtle text-fg-muted border-border-subtle",
  // Identical to `llm` TODAY, and that is the defect above, not a copy-paste.
  orchestrator: "bg-accent-subtle text-accent border-border",
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
  // Kit names, with the measured delta stated in KIND_CLASS above: the tint
  // gets stronger (37,21,32 -> 55,23,46 in dark) because the engine's
  // `warning-subtle` is a 20 % mix where the literal was 10 %.
  quality: "bg-warning-subtle text-warning border-border",
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
