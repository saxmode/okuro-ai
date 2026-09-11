// <!-- AGENT_HEADER
// role: code
// purpose: Resonance guided-interview UI — the visible half of the durable
//   interview_session. Create/resume a session, answer the tacit-gap questions
//   with a live completeness meter + turn log, check source freshness, then
//   render. Threads only a session_id (state lives server-side) so it survives a
//   reload. Drives /api/resonance/session/* + /actuality/*.
// index: SessionList | ActivePanel | InterviewSession
// AGENT_HEADER_END -->
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { Button } from "@/components/ui/button";
import {
  resonanceApi,
  type ActualityReport,
  type Session,
} from "@/lib/resonance-api";

const STATUS_CLS: Record<string, string> = {
  open: "bg-warning/15 text-warning",
  ready: "bg-success/15 text-success",
  closed: "bg-white/10 text-tertiary",
};

const inputCls =
  "w-full rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm text-fg outline-none placeholder:text-tertiary focus:border-accent/50";

/** Meter + status chip for a session's completeness. */
function Completeness({ pct, ready }: { pct: number; ready: boolean }) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="text-tertiary">Completeness</span>
        <span className="font-medium text-fg-muted">{pct}% {ready ? "· ready" : "· gathering"}</span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-white/10">
        <div className="h-full bg-accent transition-all" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function InterviewSession({ project }: { project: string }) {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [active, setActive] = useState<Session | null>(null);
  const [questions, setQuestions] = useState<string[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [actuality, setActuality] = useState<ActualityReport | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // new-session form
  const [goal, setGoal] = useState("");
  const [recipient, setRecipient] = useState("");

  const run = useCallback(async <T,>(label: string, fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(label); setError(null);
    try { return await fn(); }
    catch (e) { setError((e as Error)?.message || String(e)); }
    finally { setBusy(null); }
  }, []);

  const loadList = useCallback(async () => {
    const r = await run("list", () => resonanceApi.sessionList());
    if (r) setSessions(r.sessions.filter((s) => s.status !== "closed"));
  }, [run]);

  useEffect(() => { loadList(); }, [loadList]);

  const refresh = useCallback(async (id: string) => {
    const s = await run("get", () => resonanceApi.sessionGet(id));
    if (s) { setActive(s); setQuestions(s.open_questions || []); }
  }, [run]);

  async function start() {
    if (!goal.trim()) return;
    const s = await run("create", () => resonanceApi.sessionCreate(goal.trim(), {
      project, personId: recipient.trim() || undefined,
    }));
    if (!s) return;
    setActive(s); setAnswers({});
    const n = await run("next", () => resonanceApi.sessionNext(s.id));
    if (n) { setQuestions(n.questions); setActive({ ...s, completeness: n.completeness, ready: n.ready }); }
    setGoal(""); setRecipient("");
    loadList();
  }

  async function open(s: Session) {
    setActive(s); setAnswers({}); setActuality(null);
    await refresh(s.id);
    await run("next", () => resonanceApi.sessionNext(s.id).then((n) => {
      setQuestions(n.questions);
      setActive((cur) => cur ? { ...cur, completeness: n.completeness, ready: n.ready } : cur);
    }));
  }

  async function answer(q: string) {
    const a = answers[q]?.trim();
    if (!active || !a) return;
    const r = await run("answer", () => resonanceApi.sessionAnswer(active.id, q, a));
    if (!r) return;
    setAnswers((a) => { const c = { ...a }; delete c[q]; return c; });
    setQuestions(r.questions);
    await refresh(active.id);
  }

  async function moreQuestions() {
    if (!active) return;
    const n = await run("next", () => resonanceApi.sessionNext(active.id));
    if (n) { setQuestions(n.questions); setActive({ ...active, completeness: n.completeness, ready: n.ready }); }
  }

  async function checkActuality() {
    if (!active) return;
    const r = await run("actuality", () => resonanceApi.actualityCheck(active.artifact_ids));
    if (r) setActuality(r);
  }

  async function refreshStale() {
    if (!active) return;
    await run("refresh", () => resonanceApi.actualityRefresh(active.artifact_ids, project));
    await refresh(active.id);
    setActuality(null);
  }

  async function render() {
    if (!active) return;
    const r = await run("render", () => resonanceApi.render(
      active.goal, active.artifact_ids, active.person_id || undefined, "prism", project));
    const doc = r?.render?.result;
    if (doc?.id) navigate(`/prism?id=${encodeURIComponent(doc.id)}`);
  }

  // ── new-session + resume list ──────────────────────────────────────────────
  if (!active) {
    return (
      <div className="space-y-5">
        {error && <div className="rounded-lg border border-error/30 bg-error/10 px-4 py-3 text-sm text-error">{error}</div>}
        <div className="space-y-3">
          <textarea value={goal} onChange={(e) => setGoal(e.target.value)}
            placeholder="Goal — e.g. Convince the board to approve the AI investment"
            className={`min-h-16 resize-y ${inputCls}`} />
          <input value={recipient} onChange={(e) => setRecipient(e.target.value)}
            placeholder="Recipient (optional) — person_id or target group, e.g. marco / board"
            className={inputCls} />
          <Button size="sm" disabled={!goal.trim() || busy === "create"} onClick={start}>
            {busy === "create" ? "Starting…" : "Start interview"}
          </Button>
        </div>

        {sessions.length > 0 && (
          <div>
            <p className="mb-2 text-xs uppercase tracking-wider text-tertiary">Resume a session</p>
            <ul className="space-y-1.5">
              {sessions.map((s) => (
                <li key={s.id}>
                  <button onClick={() => open(s)}
                    className="flex w-full items-center gap-2 rounded-md border border-white/10 bg-black/20 px-3 py-2 text-left text-xs transition-colors hover:border-accent/40">
                    <span className={`shrink-0 rounded px-1.5 py-0.5 ${STATUS_CLS[s.status]}`}>{s.status}</span>
                    <span className="min-w-0 flex-1 truncate text-fg-muted">{s.goal}</span>
                    <span className="shrink-0 text-tertiary">{s.completeness != null ? `${Math.round(s.completeness * 100)}%` : "—"}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    );
  }

  // ── active session ─────────────────────────────────────────────────────────
  const pct = Math.round((active.completeness ?? 0) * 100);
  const turns = active.turns ?? [];
  return (
    <div className="space-y-5">
      {error && <div className="rounded-lg border border-error/30 bg-error/10 px-4 py-3 text-sm text-error">{error}</div>}

      <div className="flex items-start gap-3">
        <button onClick={() => { setActive(null); loadList(); }} className="shrink-0 text-xs text-tertiary hover:text-accent">← sessions</button>
        <p className="min-w-0 flex-1 text-sm text-fg-muted">{active.goal}</p>
        <span className={`shrink-0 rounded px-1.5 py-0.5 text-xs ${STATUS_CLS[active.status]}`}>{active.status}</span>
      </div>

      <Completeness pct={pct} ready={active.ready} />

      {questions.length > 0 ? (
        <div className="space-y-3">
          {questions.map((q, i) => (
            <div key={i} className="space-y-1.5 rounded-lg border border-white/10 bg-black/20 p-3">
              <p className="text-sm text-fg">{q}</p>
              <textarea value={answers[q] ?? ""} onChange={(e) => setAnswers((a) => ({ ...a, [q]: e.target.value }))}
                placeholder="Your answer…" className={`min-h-14 resize-y ${inputCls}`} />
              <Button size="sm" disabled={!answers[q]?.trim() || busy === "answer"} onClick={() => answer(q)}>
                {busy === "answer" ? "Recording…" : "Answer"}
              </Button>
            </div>
          ))}
        </div>
      ) : (
        <p className="rounded-lg border border-white/10 bg-black/20 px-3 py-3 text-sm text-tertiary">
          No open interview questions — {active.ready ? "ready to render." : "add material or check freshness."}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="secondary" disabled={busy === "next"} onClick={moreQuestions}>
          {busy === "next" ? "…" : "More questions"}
        </Button>
        <Button size="sm" variant="secondary" disabled={!active.artifact_ids.length || busy === "actuality"} onClick={checkActuality}>
          {busy === "actuality" ? "Checking…" : "Check freshness"}
        </Button>
        <Button size="sm" disabled={busy === "render"} onClick={render}>
          {busy === "render" ? "Rendering…" : "Render prism →"}
        </Button>
      </div>

      {actuality && (
        <div className="rounded-lg border border-white/10 bg-black/20 p-3 text-xs">
          <div className="mb-2 text-tertiary">{actuality.checked} source(s): {actuality.fresh} fresh · {actuality.stale.length} stale — {actuality.note}</div>
          {actuality.stale.length > 0 && (
            <>
              <ul className="space-y-1">
                {actuality.stale.map((s) => (
                  <li key={s.artifact_id} className="flex items-center gap-2">
                    <span className="rounded bg-error/15 px-1.5 py-0.5 text-error">{s.class}</span>
                    <span className="min-w-0 flex-1 truncate text-fg-muted">{s.source_ref || s.title}</span>
                    <span className="shrink-0 text-tertiary">{s.age_days}d</span>
                  </li>
                ))}
              </ul>
              <Button size="sm" variant="secondary" className="mt-2" disabled={busy === "refresh"} onClick={refreshStale}>
                {busy === "refresh" ? "Re-researching…" : "Refresh stale web sources"}
              </Button>
            </>
          )}
        </div>
      )}

      {turns.length > 0 && (
        <div>
          <p className="mb-2 text-xs uppercase tracking-wider text-tertiary">Turn log</p>
          <ul className="space-y-1.5">
            {turns.map((t, i) => (
              <li key={i} className="rounded-md bg-black/20 px-3 py-2 text-xs">
                <span className="mr-2 rounded bg-white/10 px-1.5 py-0.5 text-tertiary">{t.kind}</span>
                {t.question && <span className="text-fg-muted">{t.question}</span>}
                {t.answer && <span className="text-tertiary"> — {t.answer}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
