/**
 * RightPanel — contextual detail beside the graph.
 *
 * Three states driven by `selection`:
 *   - null / "me" → user's own cognitive + communication shape.
 *   - "person"    → person lens (markdown from /lens) + quick profile.
 *   - "edge"      → TranslateWorkspace for that person.
 *
 * Keep it presentational: data fetching stays here (queries are scoped) but
 * all mutations go to their concern components (TranslateWorkspace).
 */

import { useQuery } from "@tanstack/react-query";
import { Loader2, User as UserIcon, ArrowRightLeft, Settings } from "lucide-react";
import type { GraphSelection } from "./PeopleGraph";
import type {
  GraphNode,
  PeopleGraph,
  PersonCognitive,
} from "@/lib/people-api";
import { peopleApi } from "@/lib/people-api";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { EmptyState } from "@/components/ui/empty-state";
import { TranslateWorkspace } from "./TranslateWorkspace";
import { EnrichPanel } from "./EnrichPanel";

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full border border-border bg-surface-subtle px-2 py-0.5 text-[10px] text-fg-subtle">
      {children}
    </span>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-fg-subtle">
      {children}
    </div>
  );
}

// ── States ───────────────────────────────────────────────────────────

function EmptyHint() {
  return (
    <EmptyState
      icon={<UserIcon className="h-10 w-10" />}
      title="Pick a node or edge"
      description="Click you in the center to see your own communication shape, a person to see their lens, or an edge to translate a message for that person."
    />
  );
}

function MeDetail({ me }: { me: PeopleGraph["me"] }) {
  const comm = (me.communication || {}) as Record<string, unknown>;
  const cog = (me.cognitive || {}) as Record<string, unknown>;
  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="flex items-center gap-2 text-sm font-semibold text-fg">
          {me.display_name}
          <Chip>you</Chip>
        </div>
        {me.role && <div className="text-xs text-fg-subtle">{me.role}</div>}
      </div>

      {Object.keys(comm).length > 0 && (
        <div>
          <SectionLabel>Your communication</SectionLabel>
          <KeyValueList data={comm} />
        </div>
      )}

      {Object.keys(cog).length > 0 && (
        <div>
          <SectionLabel>Your cognition</SectionLabel>
          <KeyValueList data={cog} />
        </div>
      )}
    </div>
  );
}

function PersonDetail({ person }: { person: GraphNode }) {
  const lensQuery = useQuery({
    queryKey: ["people", "lens", person.id],
    queryFn: () => peopleApi.lens(person.id),
  });

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-auto">
      <div className="shrink-0">
        <div className="text-sm font-semibold text-fg">{person.display_name}</div>
        <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-fg-subtle">
          {person.role && <span>{person.role}</span>}
          {person.role && person.organization && <span>·</span>}
          {person.organization && <span>{person.organization}</span>}
        </div>
        {person.relation_to_user && (
          <div className="mt-1 text-xs text-fg-subtle">
            {person.relation_to_user}
          </div>
        )}
        <div className="mt-1.5 flex flex-wrap gap-1">
          {person.tags?.map((t) => (
            <Chip key={t}>{t}</Chip>
          ))}
        </div>
      </div>

      <div className="mt-3 shrink-0 rounded border border-border bg-surface-subtle p-3">
        {lensQuery.isPending ? (
          <div className="flex items-center gap-2 text-xs text-fg-subtle">
            <Loader2 className="h-3 w-3 animate-spin" />
            Loading lens…
          </div>
        ) : lensQuery.isError ? (
          <div className="text-xs text-destructive">
            {lensQuery.error instanceof Error
              ? lensQuery.error.message
              : "Failed to load lens"}
          </div>
        ) : (
          <MarkdownContent>{lensQuery.data?.markdown || ""}</MarkdownContent>
        )}
      </div>

      <ExpertisePanel cognitive={person.cognitive} />

      <EnrichPanel person={person} />
    </div>
  );
}

// Expertise & interests — CV / survey-derived fields the lens uses but never
// prints as text. Reads straight off the graph node (the /graph endpoint ships
// the full cognitive JSON per node). Renders nothing when empty.
function ExpertisePanel({ cognitive }: { cognitive?: PersonCognitive }) {
  const cog = cognitive || {};
  const profession = typeof cog.profession === "string" ? cog.profession : "";
  const func = typeof cog.function === "string" ? cog.function : "";
  const seniority = typeof cog.seniority === "string" ? cog.seniority : "";
  const interests = (cog.topic_interests || []).filter(
    (t) => t && typeof t.topic === "string",
  );
  const areas = (cog.knowledge_areas || []).filter(
    (a) => a && typeof a.area === "string",
  );

  if (!profession && !func && !seniority && !interests.length && !areas.length) {
    return null;
  }

  return (
    <div className="mt-4 flex flex-col gap-3 border-t border-border pt-4">
      <SectionLabel>Expertise &amp; Interests</SectionLabel>

      {(profession || func || seniority) && (
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-fg-subtle">
          {profession && <span className="text-fg">{profession}</span>}
          {func && <span>· {func}</span>}
          {seniority && <Chip>{seniority}</Chip>}
        </div>
      )}

      {interests.length > 0 && (
        <div>
          <div className="mb-1 text-[10px] text-fg-subtle">Interests</div>
          <div className="flex flex-wrap gap-1">
            {[...interests]
              .sort((a, b) => (b.weight || 0) - (a.weight || 0))
              .map((t, i) => (
                <Chip key={i}>
                  {t.topic}
                  {typeof t.weight === "number"
                    ? ` · ${Math.round(t.weight * 100)}%`
                    : ""}
                </Chip>
              ))}
          </div>
        </div>
      )}

      {areas.length > 0 && (
        <div>
          <div className="mb-1 text-[10px] text-fg-subtle">Knowledge areas</div>
          <dl className="space-y-1 text-xs">
            {[...areas]
              .sort((a, b) => (b.depth || 0) - (a.depth || 0))
              .map((a, i) => (
                <div key={i} className="flex items-center gap-2">
                  <dt className="flex-1 text-fg">{a.area}</dt>
                  {typeof a.depth === "number" && (
                    <dd className="shrink-0 text-fg-subtle">
                      depth {a.depth}/5
                    </dd>
                  )}
                </div>
              ))}
          </dl>
        </div>
      )}
    </div>
  );
}

function KeyValueList({ data }: { data: Record<string, unknown> }) {
  // Collapse scalar + simple-array fields into a compact list; drop nested
  // objects (presented in dedicated sections elsewhere).
  const entries = Object.entries(data)
    .filter(([, v]) => {
      if (v === null || v === undefined || v === "") return false;
      if (typeof v === "object" && !Array.isArray(v)) return false;
      return true;
    })
    .map(([k, v]) => [k, formatValue(v)] as const)
    .filter(([, v]) => v.length > 0);

  if (entries.length === 0) {
    return <div className="text-xs text-fg-subtle">—</div>;
  }
  return (
    <dl className="space-y-1 text-xs">
      {entries.map(([k, v]) => (
        <div key={k} className="flex gap-2">
          <dt className="w-28 shrink-0 text-fg-subtle capitalize">
            {k.replace(/_/g, " ")}
          </dt>
          <dd className="text-fg">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function formatValue(v: unknown): string {
  if (Array.isArray(v)) {
    return v
      .map((x) => {
        if (typeof x === "string") return x;
        if (x && typeof x === "object" && "rule" in x) {
          return String((x as { rule: unknown }).rule);
        }
        return JSON.stringify(x);
      })
      .filter(Boolean)
      .join(", ");
  }
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
    return String(v);
  }
  return "";
}

// ── Root ─────────────────────────────────────────────────────────────

export function RightPanel({
  graph,
  selection,
  onStatsInvalidate,
}: {
  graph: PeopleGraph;
  selection: GraphSelection | null;
  onStatsInvalidate?: () => void;
}) {
  const header = (icon: React.ReactNode, title: string) => (
    <div className="mb-3 flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-fg-muted">
      {icon}
      {title}
    </div>
  );

  if (!selection) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyHint />
      </div>
    );
  }

  if (selection.type === "me") {
    return (
      <div className="flex h-full flex-col overflow-auto p-5">
        {header(<UserIcon className="h-3.5 w-3.5" />, "Your profile")}
        <MeDetail me={graph.me} />
      </div>
    );
  }

  if (selection.type === "person") {
    const person = graph.nodes.find((n) => n.id === selection.id);
    if (!person) return null;
    return (
      <div className="flex h-full flex-col overflow-hidden p-5">
        {header(<Settings className="h-3.5 w-3.5" />, "Lens")}
        <PersonDetail person={person} />
      </div>
    );
  }

  // edge
  const person = graph.nodes.find((n) => n.id === selection.personId);
  if (!person) return null;
  return (
    <div className="flex h-full flex-col overflow-hidden p-5">
      {header(<ArrowRightLeft className="h-3.5 w-3.5" />, "Translate")}
      <TranslateWorkspace person={person} onTranslated={onStatsInvalidate} />
    </div>
  );
}
