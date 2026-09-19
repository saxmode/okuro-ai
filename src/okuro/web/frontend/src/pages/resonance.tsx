// <!-- AGENT_HEADER
// role: code
// purpose: /resonance route — the communication compiler UI. Intake docs →
//   gap-gate against a goal → interview for tacit gaps → render audience-fitted
//   media. Drives /api/resonance/*.
// AGENT_HEADER_END -->
import { useState, useMemo } from "react";
import { useNavigate } from "react-router";

import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/segmented";
import type { LeafViewProps } from "@/shell/views/registry";
import { sectionSlugs } from "@/shell/views/sections";
import { InterviewSession } from "@/components/resonance/interview-session";
import { useSectionTitle } from "@/shell/components/PageTitle";
import {
  resonanceApi,
  type GapReport,
  type IngestManifest,
  type Media,
  type PcoDigest,
  type ReqStatus,
} from "@/lib/resonance-api";

const PROJECT = "resonance";

interface Doc {
  id: string;
  title: string;
  claims: number;
}

const STATUS_COLOR: Record<ReqStatus, string> = {
  covered: "bg-success/15 text-success",
  partial: "bg-warning/15 text-warning",
  missing: "bg-error/15 text-error",
};

function Section({ n, title, hint, children }: {
  n: number; title: string; hint?: string; children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border-subtle bg-surface p-6">
      <div className="mb-4 flex items-baseline gap-3">
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/15 text-xs font-semibold text-accent">
          {n}
        </span>
        <h2 className="text-sm font-semibold case-label tracking-wider text-fg-muted">{title}</h2>
        {hint && <span className="text-xs text-tertiary">{hint}</span>}
      </div>
      {children}
    </section>
  );
}

/**
 * The two modes, named as the shell names its sections. The page's own value
 * for the first is `session`; the section slug is `guided`, which is the word
 * the button already says and the word the rail shows. The map is the only
 * place the two spellings meet.
 */
type ResonanceMode = "session" | "manual";

const RESONANCE_SLUGS = sectionSlugs("deliver", "resonance");
const SECTION_OF: Record<ResonanceMode, number> = {
  session: Math.max(0, RESONANCE_SLUGS.indexOf("guided")),
  manual: Math.max(0, RESONANCE_SLUGS.indexOf("manual")),
};
const MODE_OF_SECTION = (index: number): ResonanceMode =>
  index === SECTION_OF.manual ? "manual" : "session";

/**
 * THE TWO MODES ARE TWO ADDRESSES — Q-L5, ruled.
 *
 * The declared section was `["SESSIONS"]`, which rendered NOTHING: no code
 * lists sessions and `/api/resonance/sessions` has no caller, so the one label
 * in the rail named a surface that does not exist while the two modes the page
 * actually has — Guided and Manual — lived in `useState` and could not be
 * linked, bookmarked or reloaded. The manual flow (ingest, analyze, research,
 * render) was therefore unreachable by address entirely.
 *
 * SESSIONS IS RETIRED RATHER THAN BUILT. Q-L5's option B would add a session
 * list, which is ~80 new lines of feature inside a port pass, and the charter
 * puts new features out of scope. The five endpoints with no button stay
 * mirrored in the client, per Q2 option A — they are live server-side, and
 * deleting a client method that mirrors a live endpoint loses the mirror.
 */
export function ResonancePage({
  view: section,
  onSelectView,
}: Partial<LeafViewProps> = {}) {
  const navigate = useNavigate();
  /**
   * `onSelectView` IS ABSENT outside the shell and for a pane on its way out,
   * so the mode keeps a local fallback — the same reason HEALTH and MODELS
   * have one, and the same reason `pane-active` defaults to true. Without it
   * the switch would be inert on the onboarding and `?embed=1` paths.
   */
  const [localSection, setLocalSection] = useState(0);
  const mode = MODE_OF_SECTION(section ?? localSection);
  const setMode = (next: ResonanceMode) => {
    const index = SECTION_OF[next];
    if (onSelectView) onSelectView(index);
    else setLocalSection(index);
  };

  const [goal, setGoal] = useState("");
  const [docText, setDocText] = useState("");
  const [docTitle, setDocTitle] = useState("");
  const [docs, setDocs] = useState<Doc[]>([]);

  const [gap, setGap] = useState<GapReport | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});

  const [audience, setAudience] = useState("board");
  const [media, setMedia] = useState<Media>("prism");
  const [pco, setPco] = useState<PcoDigest | null>(null);
  const [rendered, setRendered] = useState<{ media: string; id?: string; url?: string } | null>(null);

  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  /* THE MODE SWITCH GOES TO THE PLATE. Q-L5 ruled the two modes are the two
     sections, and `ui/segmented` is controlled by construction — which is
     also what lets it cross the channel, where a Radix compound part cannot.
     `setMode` closes over `onSelectView`, so the prop is a dep. */
  const header = useMemo(
    () => ({
      actions: (
        <Segmented
          ariaLabel="Resonance mode"
          value={mode}
          onChange={(v) => setMode(v as ResonanceMode)}
          options={[
            { label: "Guided session", value: "session" },
            { label: "Manual", value: "manual" },
          ]}
        />
      ),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mode, onSelectView],
  );
  useSectionTitle(header);

  const ids = docs.map((d) => d.id);
  const canAnalyze = ids.length > 0 && goal.trim().length > 0;

  async function run<T>(label: string, fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(label);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function ingest() {
    const m = await run<IngestManifest>("ingest", () =>
      resonanceApi.ingest(docText.trim(), docTitle.trim() || "Untitled", undefined, PROJECT));
    if (m) {
      setDocs((d) => [...d, { id: m.artifact_id, title: docTitle.trim() || "Untitled", claims: m.claims_added }]);
      setDocText("");
      setDocTitle("");
    }
  }

  async function analyze() {
    const g = await run<GapReport>("analyze", () => resonanceApi.analyze(goal.trim(), ids, PROJECT));
    if (g) setGap(g);
  }

  async function recordAnswer(q: string) {
    const ans = (answers[q] || "").trim();
    if (!ans) return;
    const m = await run<IngestManifest>("answer", () => resonanceApi.answer(goal.trim(), q, ans, PROJECT));
    if (m) {
      setDocs((d) => [...d, { id: m.artifact_id, title: `Interview: ${q.slice(0, 32)}…`, claims: m.claims_added }]);
      setAnswers((a) => ({ ...a, [q]: "" }));
      // re-analyze with the enriched context (answer already appended to ids on next tick)
      const g = await run<GapReport>("analyze", () =>
        resonanceApi.analyze(goal.trim(), [...ids, m.artifact_id], PROJECT));
      if (g) setGap(g);
    }
  }

  async function research() {
    const r = await run("research", () => resonanceApi.research(goal.trim(), ids, 3, PROJECT));
    if (r) {
      setDocs((d) => [
        ...d,
        ...r.researched.map((x) => ({
          id: x.artifact_id,
          title: `Research: ${x.question.slice(0, 32)}…`,
          claims: x.claims_added,
        })),
      ]);
      const g = await run<GapReport>("analyze", () =>
        resonanceApi.analyze(goal.trim(), [...ids, ...r.new_artifact_ids], PROJECT));
      if (g) setGap(g);
    }
  }

  async function render() {
    setRendered(null);
    const r = await run("render", () => resonanceApi.render(goal.trim(), ids, audience.trim() || undefined, media, PROJECT));
    if (r) {
      setPco(r.pco);
      setRendered({ media: r.render.media, id: r.render.result?.id, url: r.render.result?.url });
    }
  }

  const pct = gap ? Math.round(gap.completeness * 100) : 0;

  return (
    <div className="h-full w-full overflow-y-auto">
      {/* R2 (a3a6f5ce) — THE PAGE'S OWN PADDING RING IS GONE. It was
          `px-6 py-8`, which computed to 27.6px left/right and 36.8px top INSIDE
          the shell's 55.2/36.8/0/55.2 — so the page's heading sat at x=542.8
          while the shell's title sat at x=515.2, 27.6px out of alignment and
          visible in the p3 screenshot. `space-y-6` stays; the shell's ring is
          the only gutter.

          `max-w-3xl` STAYS AND IS NOT A DEFECT: it does not bind at a 728px
          pane and it keeps an interview form readable at 1262 rather than
          stretching a question across the whole width. */}
      <div className="mx-auto max-w-3xl space-y-6">
        {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
            `<h1 class="c-title">Resonance</h1>` above this pane, so the page's
            own h1 is gone. The sentence survives as the first content block
            because it states the loop the four numbered steps belong to, which
            is what tells a reader why they are one leaf. `text-fg-muted` rather
            than `text-tertiary` (kit todo c581c9b2). */}
        <p className="type-small text-fg-muted">
          Context + a goal → audience-fitted media. Ingest your material, let the
          gate check it suffices, fill the gaps, then render.
        </p>

        {/* THE MODE SWITCH IS ON THE PLATE — see the memo above. */}
        {mode === "session" && <InterviewSession project={PROJECT} />}

        {mode === "manual" && (<>
        {error && (
          <div className="rounded-lg border border-error/30 bg-error/10 px-4 py-3 text-sm text-error">
            {error}
          </div>
        )}

        <Section n={1} title="Goal & material" hint="what you want to achieve + the documents">
          <div className="space-y-3">
            <textarea
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="Goal — e.g. Convince the board of Mobiliar to approve the AI investment"
              className="min-h-16 w-full resize-y rounded-lg border border-border-subtle bg-surface-subtle px-3 py-2 text-sm text-fg outline-none placeholder:text-tertiary focus:border-accent/50"
            />
            <input
              value={docTitle}
              onChange={(e) => setDocTitle(e.target.value)}
              placeholder="Document title"
              className="w-full rounded-lg border border-border-subtle bg-surface-subtle px-3 py-2 text-sm text-fg outline-none placeholder:text-tertiary focus:border-accent/50"
            />
            <textarea
              value={docText}
              onChange={(e) => setDocText(e.target.value)}
              placeholder="Paste document text…"
              className="min-h-28 w-full resize-y rounded-lg border border-border-subtle bg-surface-subtle px-3 py-2 text-sm text-fg outline-none placeholder:text-tertiary focus:border-accent/50"
            />
            <Button size="sm" disabled={!docText.trim() || busy === "ingest"} onClick={ingest}>
              {busy === "ingest" ? "Ingesting…" : "Ingest document"}
            </Button>
          </div>

          {docs.length > 0 && (
            <ul className="mt-4 space-y-1.5">
              {docs.map((d) => (
                <li key={d.id} className="flex items-center justify-between rounded-md bg-surface-subtle px-3 py-1.5 text-xs">
                  <span className="truncate text-fg-muted">{d.title}</span>
                  <span className="shrink-0 text-tertiary">{d.claims} claims</span>
                </li>
              ))}
            </ul>
          )}
        </Section>

        <Section n={2} title="Readiness gate" hint="does the material suffice for the goal?">
          <Button size="sm" disabled={!canAnalyze || busy === "analyze"} onClick={analyze}>
            {busy === "analyze" ? "Analyzing…" : "Analyze"}
          </Button>

          {gap && (
            <div className="mt-4 space-y-4">
              <div>
                <div className="mb-1 flex items-center justify-between text-xs">
                  <span className="text-tertiary">Completeness</span>
                  <span className="font-medium text-fg-muted">
                    {pct}% {gap.ready ? "· ready" : "· not ready"}
                  </span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-surface-subtle">
                  <div
                    className={gap.ready ? "h-full bg-success" : "h-full bg-accent"}
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </div>

              {gap.note && <p className="text-xs text-tertiary">{gap.note}</p>}

              {gap.requirements.length > 0 && (
                <ul className="space-y-1.5">
                  {gap.requirements.map((r, i) => (
                    <li key={i} className="flex items-center gap-2 text-xs">
                      <span className={`shrink-0 rounded px-1.5 py-0.5 font-medium ${STATUS_COLOR[r.status]}`}>
                        {r.status}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-fg-muted">{r.requirement}</span>
                      <span className="shrink-0 text-tertiary">{r.route}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </Section>

        {gap && gap.open_questions.length > 0 && (
          <Section n={3} title="Fill the gaps" hint="answer tacit gaps · auto-research factual ones">
            <div className="space-y-4">
              {gap.open_questions.filter((q) => q.route === "interview").map((q, i) => (
                <div key={i} className="space-y-2">
                  <p className="text-sm text-fg-muted">{q.question}</p>
                  <div className="flex gap-2">
                    <input
                      value={answers[q.question] || ""}
                      onChange={(e) => setAnswers((a) => ({ ...a, [q.question]: e.target.value }))}
                      placeholder="Your answer…"
                      className="min-w-0 flex-1 rounded-lg border border-border-subtle bg-surface-subtle px-3 py-2 text-sm text-fg outline-none placeholder:text-tertiary focus:border-accent/50"
                    />
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={!(answers[q.question] || "").trim() || busy !== null}
                      onClick={() => recordAnswer(q.question)}
                    >
                      Record
                    </Button>
                  </div>
                </div>
              ))}
              {gap.open_questions.some((q) => q.route === "research") && (
                <div className="flex flex-wrap items-center gap-3 border-t border-border-subtle pt-3">
                  <Button size="sm" variant="secondary" disabled={busy !== null} onClick={research}>
                    {busy === "research"
                      ? "Researching…"
                      : `Auto-research ${gap.open_questions.filter((q) => q.route === "research").length} factual gap(s)`}
                  </Button>
                  <span className="text-xs text-tertiary">factual/external — okuro searches the web (slow)</span>
                </div>
              )}
            </div>
          </Section>
        )}

        <Section n={4} title="Render" hint="build a provenance-tagged deck for the audience">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs text-tertiary">
              Audience (group or person id)
              <input
                value={audience}
                onChange={(e) => setAudience(e.target.value)}
                placeholder="board"
                className="w-48 rounded-lg border border-border-subtle bg-surface-subtle px-3 py-2 text-sm text-fg outline-none focus:border-accent/50"
              />
            </label>
            <div className="flex overflow-hidden rounded-lg border border-border-subtle">
              {(["prism", "slides", "website"] as Media[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setMedia(m)}
                  className={`px-3 py-2 text-xs case-label tracking-wider ${
                    media === m ? "bg-accent/20 text-accent" : "text-tertiary hover:text-fg-muted"
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>
            <Button size="sm" disabled={!canAnalyze || busy === "render"} onClick={render}>
              {busy === "render" ? "Rendering…" : "Render"}
            </Button>
          </div>

          {pco && (
            <div className="mt-4 space-y-3">
              <div className="text-xs text-tertiary">
                PCO <span className="text-fg-muted">{pco.title}</span> · {pco.beat_count} beats ·
                min confidence {pco.min_confidence.toFixed(2)}
              </div>
              <ul className="space-y-1">
                {pco.beats.map((b, i) => (
                  <li key={i} className="flex items-center gap-2 text-xs">
                    <span className="shrink-0 rounded bg-surface-subtle px-1.5 py-0.5 text-tertiary">{b.depth_hint}</span>
                    <span className="min-w-0 flex-1 truncate text-fg-muted">{b.intent}</span>
                    <span className="shrink-0 text-tertiary">{b.claims.length} claims</span>
                  </li>
                ))}
              </ul>
              {(rendered?.id || rendered?.url) && (
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() =>
                    rendered.media === "website" && rendered.url
                      ? window.open(rendered.url, "_blank")
                      : navigate(`/${rendered.media}?id=${encodeURIComponent(rendered.id!)}`)
                  }
                >
                  Open {rendered.media} →
                </Button>
              )}
            </div>
          )}
        </Section>
        </>)}
      </div>
    </div>
  );
}
