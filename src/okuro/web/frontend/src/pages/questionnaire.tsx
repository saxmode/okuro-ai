/**
 * /q/:token — public self-report questionnaire.
 *
 * Standalone page — NO AppShell, NO AuthGate, NO sidebar. Recipient may
 * never have seen the Okuro UI before, so this is a deliberately minimal,
 * single-focus flow. Styling follows the Okuro token set.
 *
 * Two flavours, chosen by the server's `structured` flag:
 *   - structured: render survey_form() → knowledge swipe (with hidden foils)
 *     + depth taps + forced-choice pairs + REI sliders → POST {survey}.
 *     Scored deterministically server-side (no LLM).
 *   - legacy free-text: the original textarea questions → POST {responses}.
 *
 * Both consume endpoints are bearer-exempt (see _AUTH_EXEMPT_PREFIXES in
 * orchestrator/api/main.py).
 */

import { useState } from "react";
import { useParams } from "react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Loader2, Check, AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  questionnaireApi,
  type SurveyForm,
  type SurveyStep,
  type StructuredSurveyResponse,
} from "@/lib/people-api";

export function QuestionnairePage() {
  const { token } = useParams<{ token: string }>();
  const [submitted, setSubmitted] = useState<null | { count: number }>(null);

  const q = useQuery({
    queryKey: ["questionnaire", token],
    queryFn: () => questionnaireApi.get(token || ""),
    enabled: Boolean(token),
    retry: false,
  });

  if (!token) {
    return <ErrorShell title="Missing link" detail="The link is incomplete." />;
  }
  if (q.isPending) {
    return (
      <Shell>
        <div className="flex items-center gap-2 text-sm text-fg-subtle">
          <Loader2 className="h-4 w-4 animate-spin" />
          Opening your link…
        </div>
      </Shell>
    );
  }
  if (q.isError) {
    const msg = q.error instanceof Error ? q.error.message : "Unknown error";
    return (
      <ErrorShell
        title={msg.toLowerCase().includes("expired") ? "Link expired" : "Link not found"}
        detail={msg}
      />
    );
  }

  const data = q.data!;
  // The shell speaks the same language as the form inside it. These keys
  // existed in the catalogue from the start and nothing was reading them.
  const ui = data.survey_form?.ui ?? {};
  const t = (k: string, fallback: string) => ui[k] ?? fallback;

  if (submitted) {
    return (
      <Shell>
        <div className="mb-6 flex items-start gap-3 rounded-md border border-accent/30 bg-accent/10 p-4">
          <Check className="mt-0.5 h-5 w-5 shrink-0 text-accent" />
          <div>
            <div className="text-sm font-semibold text-fg">
              {t("thanks_title", "Thanks — got it.")}
            </div>
            <div className="mt-1 text-xs text-fg-subtle">
              {t("thanks_body", "Your answers shape how messages are written for you from here on.")}
            </div>
          </div>
        </div>
      </Shell>
    );
  }

  if (data.already_answered) {
    return (
      <Shell>
        <div className="flex items-start gap-3 rounded-md border border-border bg-surface-subtle p-4">
          <Check className="mt-0.5 h-5 w-5 shrink-0 text-accent" />
          <div>
            <div className="text-sm font-semibold text-fg">
              {t("already_title", "Already filled in")}
            </div>
            <div className="mt-1 text-xs text-fg-subtle">
              {t("already_body", "You've answered this before. Nothing more needed.")}
            </div>
          </div>
        </div>
      </Shell>
    );
  }

  const header = (
    <div className="mb-6">
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-subtle">
        {t("eyebrow", "Communication profile")}
      </div>
      <div className="mt-1 text-2xl font-semibold text-fg">
        {data.person_display_name}
      </div>
      {data.person_role && (
        <div className="mt-0.5 text-xs text-fg-subtle">{data.person_role}</div>
      )}
    </div>
  );

  // Tell the document what language it is in, not just the strings: a
  // screen reader pronounces the page according to <html lang>, and a
  // German survey declared as English is read aloud in English.
  if (data.survey_form?.lang) {
    document.documentElement.lang = data.survey_form.lang;
  }

  if (data.structured && data.survey_form) {
    return (
      <Shell>
        {header}
        <StructuredSurvey
          token={token}
          form={data.survey_form}
          onDone={(count) => setSubmitted({ count })}
        />
      </Shell>
    );
  }

  return (
    <Shell>
      {header}
      <FreeTextSurvey
        token={token}
        questions={data.questions}
        onDone={() => setSubmitted({ count: 0 })}
      />
    </Shell>
  );
}

// ── Structured survey v2 (profession-agnostic lens) ───────────────────
//
// Choice-first, one-thing-per-screen flow per ui-spec.md. Forced-choice
// cards replace raw sliders for cognitive/angle. Cognitive/angle init to
// null (never 2) and null axes are stripped before POST, so the scorer
// never records an untouched axis as a confident datum.

type Area = {
  area: string;
  depth: number | null;
  recency: string;
  detail: string;
};
type Weak = { area: string; mode: "explain" | "skip" };

const inputCls =
  "w-full rounded-md border border-border bg-surface-subtle px-3 py-2 text-sm text-fg outline-none focus:border-accent/60";

// Plain-language card option. `value` is written verbatim into cog/ang state
// and is NEVER translated — it is the datum the scorer reads. Only label and
// helper are language-dependent, and both arrive from survey_form(lang).
type CardOption = { value: number; label: string; helper: string };

// Per-axis card definitions. The id + value range match survey_form();
// only the rendered copy is reworded for plain language (ui-spec §3).
const cardLabel = (cards: CardOption[], v: number | null): string | null =>
  v === null ? null : (cards.find((c) => c.value === v)?.label ?? null);

// Step order, names and the core/module tier come from survey_form()['steps']
// (peer/survey.py::_STEPS) — never restated here. A second copy is exactly the
// drift tests/peer/test_survey_form_parity.py exists to refuse. The depth loop
// is one logical step with its own internal per-area sub-navigation.

function confidenceCaption(v: number, labels?: Record<string, unknown>): string {
  const L = (k: string, fallback: string) => String(labels?.[k] ?? fallback);
  if (v <= 33) return L("low", "rough guess");
  if (v <= 66) return L("mid", "fairly sure");
  return L("high", "very sure");
}

export function StructuredSurvey({
  token,
  form,
  onDone,
}: {
  token: string;
  form: SurveyForm;
  onDone: (count: number) => void;
}) {
  const steps = form.steps ?? [];
  const [stepIdx, setStepIdx] = useState(0);
  const step = steps[stepIdx];

  // The progressive-module gate. null = not asked yet, false = "I'm done".
  const [wantModules, setWantModules] = useState<boolean | null>(null);

  // Copy lookups. Everything a recipient reads comes from survey_form(lang);
  // the fallbacks exist only so a pre-i18n server still renders, and are the
  // ONLY English literals left in this file.
  const t = (k: string, fallback: string) => form.ui?.[k] ?? fallback;
  const itemById = new Map(
    [...form.cognitive, ...form.angle].map((s) => [s.id, s] as const),
  );
  const cardsFor = (axis: string): CardOption[] => itemById.get(axis)?.cards ?? [];
  const isAngle = (axis: string) => form.angle.some((a) => a.id === axis);
  const valueFor = (axis: string): number | null =>
    (isAngle(axis) ? ang[axis] : cog[axis]) ?? null;
  const setAxis = (axis: string, v: number) =>
    isAngle(axis)
      ? setAng((c) => ({ ...c, [axis]: v }))
      : setCog((c) => ({ ...c, [axis]: v }));

  // Identity
  const [profession, setProfession] = useState("");
  const [fn, setFn] = useState("");
  const [seniority, setSeniority] = useState("");

  // Knowledge
  const [areas, setAreas] = useState<Area[]>([]);
  const [newArea, setNewArea] = useState("");
  const [depthIdx, setDepthIdx] = useState(0); // sub-cursor inside the depth loop
  const [weaknesses, setWeaknesses] = useState<Weak[]>([]);
  const [newWeak, setNewWeak] = useState("");

  // Confidence — null until the slider is touched (no silent 50).
  const [confidence, setConfidence] = useState<number | null>(null);

  // Cognitive / angle — null = untouched (never sent as confident data).
  const [cog, setCog] = useState<Record<string, number | null>>(() =>
    Object.fromEntries(form.cognitive.map((s) => [s.id, null])),
  );
  const [ang, setAng] = useState<Record<string, number | null>>(() =>
    Object.fromEntries(form.angle.map((s) => [s.id, null])),
  );

  const submit = useMutation({
    mutationFn: (survey: StructuredSurveyResponse) =>
      questionnaireApi.submitSurvey(token, survey),
    onSuccess: (d) => onDone((d.fields_updated || []).length),
  });

  function addArea(name: string) {
    const n = name.trim();
    if (!n || areas.some((a) => a.area.toLowerCase() === n.toLowerCase())) return;
    setAreas((a) => [...a, { area: n, depth: null, recency: "current", detail: "" }]);
  }
  function toggleArea(name: string) {
    const exists = areas.some((a) => a.area.toLowerCase() === name.toLowerCase());
    if (exists) {
      setAreas((a) => a.filter((x) => x.area.toLowerCase() !== name.toLowerCase()));
    } else {
      addArea(name);
    }
  }
  function patchArea(i: number, patch: Partial<Area>) {
    setAreas((x) => x.map((y, j) => (j === i ? { ...y, ...patch } : y)));
  }
  function removeArea(i: number) {
    setAreas((x) => x.filter((_, j) => j !== i));
    setDepthIdx((d) => (d >= i && d > 0 ? d - 1 : d));
  }
  function addWeak(name: string) {
    const n = name.trim();
    if (!n || weaknesses.some((w) => w.area.toLowerCase() === n.toLowerCase())) return;
    setWeaknesses((w) => [...w, { area: n, mode: "explain" }]);
  }

  function finish() {
    const cognitive = Object.fromEntries(
      Object.entries(cog).filter(([, v]) => v !== null),
    ) as Record<string, number>;
    const angle = Object.fromEntries(
      Object.entries(ang).filter(([, v]) => v !== null),
    ) as Record<string, number>;

    const areasOut = areas
      .filter((a) => a.depth !== null)
      .map((a) => {
        const out: { area: string; depth: number; recency: string; detail?: string } = {
          area: a.area,
          depth: a.depth as number,
          recency: a.recency,
        };
        if (a.detail.trim()) out.detail = a.detail.trim();
        return out;
      });

    const knowledge: StructuredSurveyResponse["knowledge"] = {
      areas: areasOut,
      weaknesses,
    };
    if (confidence !== null) knowledge.confidence = confidence;

    submit.mutate({
      identity: { profession, function: fn, seniority },
      knowledge,
      cognitive,
      angle,
    });
  }

  const hasAreas = areas.length > 0;

  // A step is skipped when it cannot apply: the depth loop with no areas, and
  // every progressive module once the recipient has said they're done. Both
  // reasons funnel through one predicate so forward and backward navigation
  // can never disagree about what exists.
  function visible(s: SurveyStep, want: boolean | null): boolean {
    if (s.tier === "module" && want === false) return false;
    if (s.id === "depth" && !hasAreas) return false;
    return true;
  }
  function walk(from: number, dir: 1 | -1, want: boolean | null): number {
    let i = from + dir;
    while (i > 0 && i < steps.length - 1 && !visible(steps[i]!, want)) i += dir;
    return Math.max(0, Math.min(i, steps.length - 1));
  }
  function goNext(want: boolean | null = wantModules) {
    setStepIdx((i) => walk(i, 1, want));
  }
  function goBack() {
    setStepIdx((i) => walk(i, -1, wantModules));
  }
  // The gate answers and advances in one action, so the walk must see the new
  // answer rather than the state value this render closed over.
  function answerGate(want: boolean) {
    setWantModules(want);
    goNext(want);
  }

  // Depth-loop internal navigation: advance within areas, else step out.
  function depthNext() {
    if (depthIdx < areas.length - 1) setDepthIdx((d) => d + 1);
    else goNext();
  }
  function depthBack() {
    if (depthIdx > 0) setDepthIdx((d) => d - 1);
    else goBack();
  }

  // Progress counts only the steps this recipient will actually see, so
  // declining the modules shortens the bar instead of stranding it at 30%.
  const shown = steps.filter((s) => visible(s, wantModules));
  const total = shown.length;
  const shownIdx = step ? shown.findIndex((s) => s.id === step.id) : 0;

  // ── Per-step "answered / can advance" gates ──────────────────────────
  const currentArea = areas[depthIdx];
  const depthAnswered = currentArea ? currentArea.depth !== null : true;
  const decideAnswered =
    cog["rational"] !== null && cog["experiential"] !== null;

  // ── Render ───────────────────────────────────────────────────────────
  if (!step) {
    return (
      <p className="text-sm text-fg-subtle">
        {t("unavailable", "This form is unavailable — the server sent no steps.")}
      </p>
    );
  }
  const isIntro = step.id === "intro";
  const isRecap = step.id === "recap";
  const isDepth = step.id === "depth";
  const isGate = step.id === "more";
  // A single-axis card screen. The depth loop also measures nothing directly
  // and has its own per-area sub-navigation, so it is excluded by id.
  const isCards = step.axes.length === 1 && step.id !== "depth";

  return (
    <div>
      {!isIntro && (
        <ProgressBar
          stepIdx={Math.max(0, shownIdx)}
          total={total}
          name={step.name}
          template={t("step_of", "Step {n} of {total}")}
        />
      )}
      <div className="mt-5">
        {step.id === "intro" && (
          <Panel title={step.heading ?? ""} hint="">
            <p className="text-sm leading-relaxed text-fg-subtle">{step.hint}</p>
            {/* Arrow-wrapped: goNext's first parameter is the module answer,
                and a bare handler would hand it a MouseEvent. */}
            <Button className="mt-2" onClick={() => goNext()}>{t("start", "Start")}</Button>
          </Panel>
        )}

        {step.id === "you" && (
          <Panel title={step.heading ?? ""} hint={step.hint ?? ""}>
            <Field label={String(step.profession_label ?? "")}>
              <input
                className={inputCls}
                value={profession}
                onChange={(e) => setProfession(e.target.value)}
                placeholder={String(step.profession_placeholder ?? "")}
              />
            </Field>
            <div className="-mt-1 ml-3 border-l border-border pl-3">
              <Field label={String(step.function_label ?? "")}>
                <Select value={fn} onChange={setFn} options={form.identity.function_options} />
              </Field>
            </div>
            <Field label={String(step.seniority_label ?? "")}>
              {/* value = the stable key the scorer reads, label = translated.
                  Sending the label would make the answer depend on which
                  language the recipient happened to read. */}
              <Select
                value={seniority}
                onChange={setSeniority}
                options={form.identity.seniority_options}
                labels={form.identity.seniority_labels}
              />
            </Field>
          </Panel>
        )}

        {isGate && (
          <Panel
            title={step.heading ?? ""}
            hint={step.hint ?? ""}
          >
            <div className="flex flex-col gap-2">
              {(form.modules ?? []).map((m) => (
                <div
                  key={m.key}
                  className="rounded-md border border-border bg-surface-subtle px-3 py-2"
                >
                  <div className="text-sm font-medium text-fg">{m.name}</div>
                  <div className="mt-0.5 text-xs text-fg-subtle">{m.blurb}</div>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => answerGate(true)}>
                {t("keep_going", "Keep going")}
              </Button>
              <Button variant="outline" onClick={() => answerGate(false)}>
                {t("im_done", "")}
              </Button>
            </div>
          </Panel>
        )}

        {step.id === "areas" && (
          <Panel title={step.heading ?? ""} hint={step.hint ?? ""}>
            <div className="flex flex-wrap gap-2">
              {form.knowledge.starter_domains.map((d) => (
                <Chip key={d} active={areas.some((a) => a.area === d)} onClick={() => toggleArea(d)}>
                  {d}
                </Chip>
              ))}
            </div>
            <div className="mt-3 flex gap-2">
              <input
                className={inputCls}
                value={newArea}
                onChange={(e) => setNewArea(e.target.value)}
                placeholder={t("type_and_enter", "")}
                onKeyDown={(e) => {
                  if (e.key === "Enter") { addArea(newArea); setNewArea(""); }
                }}
              />
              <Button variant="outline" onClick={() => { addArea(newArea); setNewArea(""); }}>{t("add", "Add")}</Button>
            </div>
            {!hasAreas && (
              <p className="mt-3 text-xs text-fg-muted">{t("no_areas", "")}</p>
            )}
          </Panel>
        )}

        {isDepth && currentArea && (
          <div className="flex flex-col gap-4">
            <div>
              <div className="flex items-start justify-between gap-3">
                <div className="text-sm font-semibold text-fg">
                  {String(step.heading ?? "").replace("{area}", currentArea.area)}
                </div>
                <div className="shrink-0 text-xs text-fg-subtle">
                  {t("of_count", "{i} of {n}")
                    .replace("{i}", String(depthIdx + 1))
                    .replace("{n}", String(areas.length))}
                </div>
              </div>
              <div className="mt-0.5 text-xs text-fg-subtle">{step.hint}</div>
            </div>

            <div className="flex flex-col gap-2">
              {form.knowledge.depth_anchors.map((label, di) => (
                <CardButton
                  key={di}
                  selected={currentArea.depth === di}
                  label={label}
                  helper={form.knowledge.depth_helpers?.[di] ?? ""}
                  onClick={() => patchArea(depthIdx, { depth: di })}
                />
              ))}
            </div>

            <button
              type="button"
              className="self-start text-xs text-fg-subtle underline-offset-2 hover:text-fg hover:underline"
              onClick={() => removeArea(depthIdx)}
            >
              {t("skip_area", "")}
            </button>

            <div>
              <div className="mb-1.5 text-xs text-fg-subtle">
                {String(step.recency_label ?? "")}
              </div>
              <PillGroup
                options={form.knowledge.recency_options.map((v) => ({
                  value: v,
                  label: form.knowledge.recency_labels?.[v] ?? v,
                }))}
                value={currentArea.recency}
                onChange={(v) => patchArea(depthIdx, { recency: v })}
              />
            </div>

            <Field label={form.knowledge.detail_prompt}>
              <input
                className={inputCls}
                value={currentArea.detail}
                onChange={(e) => patchArea(depthIdx, { detail: e.target.value })}
              />
            </Field>
          </div>
        )}

        {step.id === "weak" && (
          <Panel
            title={step.heading ?? ""}
            hint={step.hint ?? ""}
          >
            <div className="flex gap-2">
              <input
                className={inputCls}
                value={newWeak}
                onChange={(e) => setNewWeak(e.target.value)}
                placeholder={String(step.placeholder ?? "")}
                onKeyDown={(e) => {
                  if (e.key === "Enter") { addWeak(newWeak); setNewWeak(""); }
                }}
              />
              <Button variant="outline" onClick={() => { addWeak(newWeak); setNewWeak(""); }}>Add</Button>
            </div>
            <div className="flex flex-col gap-2">
              {weaknesses.map((w, i) => (
                <div
                  key={w.area}
                  className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface-subtle p-2"
                >
                  <span className="mr-auto text-sm text-fg">{w.area}</span>
                  <PillGroup
                    options={[
                      { value: "explain", label: String(step.explain ?? "") },
                      { value: "skip", label: String(step.skip ?? "") },
                    ]}
                    value={w.mode}
                    onChange={(v) =>
                      setWeaknesses((x) =>
                        x.map((y, j) => (j === i ? { ...y, mode: v as Weak["mode"] } : y)),
                      )
                    }
                  />
                  <button
                    type="button"
                    aria-label={t("remove", "Remove {name}").replace("{name}", w.area)}
                    className="flex h-11 w-11 items-center justify-center rounded-md text-fg-subtle hover:text-fg"
                    onClick={() => setWeaknesses((x) => x.filter((_, j) => j !== i))}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
            {weaknesses.length === 0 && (
              <p className="text-xs text-fg-muted">{t("nothing_here", "")}</p>
            )}
          </Panel>
        )}

        {/* Every single-axis card screen, rendered once. Heading, hint and
            the cards themselves come from survey_form(lang) — this file
            holds no recipient-facing sentence any more. */}
        {isCards && (
          <CardScreen
            heading={step.heading ?? ""}
            hint={step.hint ?? ""}
            cards={cardsFor(step.axes[0]!)}
            value={valueFor(step.axes[0]!)}
            onChange={(v) => setAxis(step.axes[0]!, v)}
          />
        )}

        {/* The one screen that measures two axes at once: REI's poles are
            orthogonal, so they are asked together to signal that both can
            be high. */}
        {step.id === "decide" && (
          <Panel title={step.heading ?? ""} hint={step.hint ?? ""}>
            {step.axes.map((axis, i) => (
              <div key={axis}>
                <div className="mb-2 text-xs font-medium uppercase tracking-wider text-fg-subtle">
                  {String(step[i === 0 ? "group_rational" : "group_experiential"] ?? "")}
                </div>
                <CardList
                  cards={cardsFor(axis)}
                  value={valueFor(axis)}
                  onChange={(v) => setAxis(axis, v)}
                />
              </div>
            ))}
          </Panel>
        )}


        {step.id === "confidence" && (
          <Panel
            title={step.heading ?? ""}
            hint={step.hint ?? ""}
          >
            <ConfidenceSlider
              value={confidence}
              onChange={setConfidence}
              labels={step as Record<string, unknown>}
            />
          </Panel>
        )}

        {isRecap && (
          <Panel title={step.heading ?? ""} hint={step.hint ?? ""}>
            <Recap
              form={form}
              step={step}
              profession={profession}
              fn={fn}
              seniority={seniority}
              areas={areas}
              weaknesses={weaknesses}
              cog={cog}
              ang={ang}
              confidence={confidence}
            />
          </Panel>
        )}

        {/* Footer nav — intro and the module gate carry their own buttons */}
        {!isIntro && (
          <div className="mt-7 flex items-center justify-between gap-3">
            <Button variant="outline" onClick={isDepth ? depthBack : goBack}>
              {t("back", "Back")}
            </Button>
            {isRecap ? (
              <Button disabled={submit.isPending} onClick={finish}>
                {submit.isPending ? (
                  <><Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />{t("saving", "Saving…")}</>
                ) : (
                  t("finish", "Finish")
                )}
              </Button>
            ) : isGate ? null : isDepth ? (
              <Button disabled={!depthAnswered} onClick={depthNext}>
                {depthIdx < areas.length - 1 ? t("next_area", "Next area") : t("next", "Next")}
              </Button>
            ) : (
              <Button
                disabled={step.id === "decide" && !decideAnswered}
                onClick={() => goNext()}
              >
                {t("next", "Next")}
              </Button>
            )}
          </div>
        )}
        {submit.isError && (
          <div className="mt-3 text-xs text-destructive">
            {submit.error instanceof Error ? submit.error.message : "Failed"}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Recap summary (read-only, only answered items) ────────────────────

function Recap({
  form,
  step,
  profession,
  fn,
  seniority,
  areas,
  weaknesses,
  cog,
  ang,
  confidence,
}: {
  form: SurveyForm;
  step: SurveyStep;
  profession: string;
  fn: string;
  seniority: string;
  areas: Area[];
  weaknesses: Weak[];
  cog: Record<string, number | null>;
  ang: Record<string, number | null>;
  confidence: number | null;
}) {
  /* Built from the catalogue, so the recap speaks whatever language the
     rest of the form does. The axis ORDER comes from the steps, so a new
     axis appears here automatically instead of needing a line added. */
  const L = (k: string) => String(step[k] ?? "");
  // ui.*, not step.* — a recap key looked up on the step resolves to "" and
  // falls back to a bare dash, which reads as "nothing" rather than "not
  // answered" and is the same in every language.
  const naText = form.ui?.["not_answered"] ?? "—";
  const na = <span className="text-fg-muted">{naText}</span>;
  const lines: React.ReactNode[] = [];

  const itemById = new Map(
    [...form.cognitive, ...form.angle].map((s) => [s.id, s] as const),
  );
  const labelFor = (axis: string) => {
    const v = (ang[axis] ?? cog[axis]) ?? null;
    return cardLabel(itemById.get(axis)?.cards ?? [], v);
  };

  const identityBits = [
    profession.trim() && `${profession.trim()}`,
    fn && L("closest_to").replace("{fn}", fn),
    seniority && seniority,
  ].filter(Boolean);
  if (identityBits.length) {
    lines.push(<>{L("you_are").replace("{bits}", identityBits.join(", "))}</>);
  }

  const deep = areas
    .filter((a) => a.depth !== null && (a.depth as number) >= 3)
    .map((a) => a.area);
  if (deep.length) lines.push(<>{L("deep_in").replace("{areas}", deep.join(", "))}</>);

  const explain = weaknesses.filter((w) => w.mode === "explain").map((w) => w.area);
  const skip = weaknesses.filter((w) => w.mode === "skip").map((w) => w.area);
  if (explain.length)
    lines.push(<>{L("want_explained").replace("{areas}", explain.join(", "))}</>);
  if (skip.length) lines.push(<>{L("skip").replace("{areas}", skip.join(", "))}</>);

  /* One labelled line per measured axis, in step order. */
  const RECAP_LABEL: Record<string, string> = {
    need_for_cognition: "reasoning",
    visual_verbal: "format",
    density: "density",
    ambiguity: "uncertainty",
    numeracy: "numbers",
    graph_literacy: "charts",
    construal: "focus",
    regulatory_focus: "framing",
  };
  for (const st of form.steps ?? []) {
    if (st.id === "decide") {
      lines.push(
        <>
          {L("decisions")
            .replace("{a}", labelFor("rational") ?? naText)
            .replace("{b}", labelFor("experiential") ?? naText)}
        </>,
      );
      continue;
    }
    for (const axis of st.axes) {
      const key = RECAP_LABEL[axis];
      if (!key) continue;
      const val = labelFor(axis);
      lines.push(
        <>
          {L(key)}: {val ?? na}.
        </>,
      );
    }
  }

  if (confidence !== null) {
    lines.push(<>{L("confidence")}: {confidenceCaption(confidence, step)}.</>);
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border bg-surface-subtle p-3 text-sm text-fg-subtle">
      {lines.map((l, i) => (
        <div key={i}>{l}</div>
      ))}
    </div>
  );
}

// ── Reusable card / pill / panel components ───────────────────────────

function CardScreen({
  heading,
  hint,
  cards,
  value,
  onChange,
}: {
  heading: string;
  hint: string;
  cards: CardOption[];
  value: number | null;
  onChange: (v: number) => void;
}) {
  return (
    <Panel title={heading} hint={hint}>
      <CardList cards={cards} value={value} onChange={onChange} />
    </Panel>
  );
}

function CardList({
  cards,
  value,
  onChange,
}: {
  cards: CardOption[];
  value: number | null;
  onChange: (v: number) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      {cards.map((c) => (
        <CardButton
          key={c.value}
          selected={value === c.value}
          label={c.label}
          helper={c.helper}
          onClick={() => onChange(c.value)}
        />
      ))}
    </div>
  );
}

function CardButton({
  selected,
  label,
  helper,
  onClick,
}: {
  selected: boolean;
  label: string;
  helper?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={
        "flex min-h-[48px] w-full items-start gap-2 rounded-md border px-3 py-3 text-left transition-colors " +
        (selected
          ? "border-accent bg-accent/15 text-fg"
          : "border-border bg-surface-subtle text-fg-subtle hover:text-fg")
      }
    >
      <Check
        className={"mt-0.5 h-4 w-4 shrink-0 " + (selected ? "text-accent" : "text-transparent")}
      />
      <span>
        <span className="block text-sm font-medium text-fg">{label}</span>
        {helper && <span className="mt-0.5 block text-xs text-fg-subtle">{helper}</span>}
      </span>
    </button>
  );
}

function PillGroup({
  options,
  value,
  onChange,
}: {
  options: Array<{ value: string; label: string }>;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {options.map((o) => {
        const active = value === o.value;
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(o.value)}
            className={
              "inline-flex min-h-[44px] items-center gap-1 rounded-full border px-3 text-xs transition-colors " +
              (active
                ? "border-accent bg-accent/15 text-fg"
                : "border-border bg-surface-subtle text-fg-subtle hover:text-fg")
            }
          >
            {active && <Check className="h-3 w-3 text-accent" />}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

function ConfidenceSlider({
  value,
  onChange,
  labels,
}: {
  value: number | null;
  onChange: (v: number) => void;
  labels?: Record<string, unknown>;
}) {
  const L = (k: string, fallback: string) => String(labels?.[k] ?? fallback);
  const touched = value !== null;
  const display = value ?? 50;
  return (
    <label className="block">
      <input
        type="range"
        min={0}
        max={100}
        step={5}
        value={display}
        onChange={(e) => onChange(Number(e.target.value))}
        className={"w-full accent-accent transition-opacity " + (touched ? "" : "opacity-40")}
      />
      <div className="mt-2 flex items-center justify-between gap-3 text-sm text-fg-muted">
        <span className="leading-tight">{L("low", "rough guess")}</span>
        {touched && (
          <span className="font-semibold text-accent">{confidenceCaption(value, labels)}</span>
        )}
        <span className="text-right leading-tight">{L("high", "very sure")}</span>
      </div>
      {!touched && (
        <div className="mt-1 text-xs text-fg-muted">{L("untouched", "")}</div>
      )}
    </label>
  );
}

function Panel({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="text-sm font-semibold text-fg">{title}</div>
        {hint && <div className="mt-0.5 text-xs text-fg-subtle">{hint}</div>}
      </div>
      {children}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="mb-1 text-xs text-fg-subtle">{label}</div>
      {children}
    </label>
  );
}

function Select({
  value,
  onChange,
  options,
  labels,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  /** Optional key -> display text. Absent means the option IS its label. */
  labels?: Record<string, string>;
}) {
  return (
    <select className={inputCls} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">—</option>
      {options.map((o) => (
        <option key={o} value={o}>{labels?.[o] ?? o}</option>
      ))}
    </select>
  );
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={
        "inline-flex min-h-[36px] items-center gap-1 rounded-full border px-3 py-2 text-xs transition-colors " +
        (active
          ? "border-accent bg-accent/15 text-fg"
          : "border-border bg-surface-subtle text-fg-subtle hover:text-fg")
      }
    >
      {active && <Check className="h-3 w-3 text-accent" />}
      {children}
    </button>
  );
}

function ProgressBar({
  stepIdx,
  total,
  name,
  template,
}: {
  stepIdx: number;
  total: number;
  name: string;
  /* "Step {n} of {total}" — translated; German is "Schritt {n} von {total}". */
  template: string;
}) {
  const pct = Math.round(((stepIdx + 1) / total) * 100);
  const label = template
    .replace("{n}", String(stepIdx + 1))
    .replace("{total}", String(total));
  return (
    <div>
      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-fg-subtle">
        {label} — {name}
      </div>
      <div className="h-1 w-full overflow-hidden rounded-full bg-surface-subtle">
        <div className="h-full bg-accent transition-all duration-300" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

// ── Legacy free-text survey ───────────────────────────────────────────

function FreeTextSurvey({
  token,
  questions,
  onDone,
}: {
  token: string;
  questions: Array<{ id: string; prompt: string }>;
  onDone: () => void;
}) {
  const [responses, setResponses] = useState<Record<string, string>>({});
  const submit = useMutation({
    mutationFn: () => questionnaireApi.submit(token, responses),
    onSuccess: () => onDone(),
  });
  const allAnswered = questions.every(
    (q) => (responses[q.id] || "").trim().length > 0,
  );

  return (
    <div>
      <p className="mb-6 text-xs text-fg-muted">
        Answer in your own words — there are no wrong answers.
      </p>
      <div className="flex flex-col gap-5">
        {questions.map((q) => (
          <div key={q.id}>
            <label className="block text-xs font-medium text-fg">{q.prompt}</label>
            <Textarea
              value={responses[q.id] || ""}
              onChange={(e) =>
                setResponses((r) => ({ ...r, [q.id]: e.target.value }))
              }
              rows={2}
              className="mt-1 text-sm"
            />
          </div>
        ))}
      </div>
      <div className="mt-6 flex items-center justify-between">
        <div className="text-[10px] text-fg-subtle">
          {Object.values(responses).filter((v) => v.trim()).length} /{" "}
          {questions.length} answered
        </div>
        <Button disabled={!allAnswered || submit.isPending} onClick={() => submit.mutate()}>
          {submit.isPending ? (
            <>
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              Submitting…
            </>
          ) : (
            "Submit"
          )}
        </Button>
      </div>
      {submit.isError && (
        <div className="mt-3 text-xs text-destructive">
          {submit.error instanceof Error ? submit.error.message : "Failed"}
        </div>
      )}
    </div>
  );
}

// ── Shell helpers — minimal centered layout, no sidebar ───────────────

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-surface text-fg">
      <div className="mx-auto w-full max-w-xl px-5 py-10">{children}</div>
    </div>
  );
}

function ErrorShell({ title, detail }: { title: string; detail?: string }) {
  return (
    <Shell>
      <div className="flex items-start gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-4">
        <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
        <div>
          <div className="text-sm font-semibold text-fg">{title}</div>
          {detail && <div className="mt-1 text-xs text-fg-subtle">{detail}</div>}
        </div>
      </div>
    </Shell>
  );
}
