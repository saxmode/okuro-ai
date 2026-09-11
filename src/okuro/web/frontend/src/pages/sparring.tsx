// <!-- AGENT_HEADER
// role: code
// purpose: /sparring route — the stateful prism SPARRING partner. Bare shows the
//   session list + new-session starter; ?id= opens a session: the turn TIMELINE
//   (left) + the derived LIVE BOARD and MOVE bar (right). Change-feed poll keeps
//   an open session live. The partner that remembers — recall turns surface prior
//   sessions' assumptions/decisions on the same topic.
// AGENT_HEADER_END -->
import { AlertTriangle, ArrowLeft, Ban, Check, Clock, Plus, Swords, Trash2, X, Zap } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";

import {
  sparringApi,
  type Move,
  type MoveType,
  type SessionDetail,
  type SessionSummary,
  type Turn,
  type Verdict,
} from "@/lib/sparring-api";

// ---------------------------------------------------------------------------
// Route entry — list vs. one session
// ---------------------------------------------------------------------------

export function SparringPage() {
  const [params] = useSearchParams();
  const id = params.get("id");
  return (
    <div className="relative h-full w-full overflow-hidden">
      {id ? <SessionView sessionId={id} /> : <SessionList />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Session list + starter
// ---------------------------------------------------------------------------

function SessionList() {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [topic, setTopic] = useState("");
  const [person, setPerson] = useState("");
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await sparringApi.list();
      setSessions(res.sessions);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function start() {
    const t = topic.trim();
    if (!t || starting) return;
    setStarting(true);
    setError(null);
    try {
      const s = await sparringApi.start(t, person.trim() || undefined);
      navigate(`/sparring?id=${encodeURIComponent(s.id)}`);
    } catch (e) {
      setError(String(e));
      setStarting(false);
    }
  }

  async function remove(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await sparringApi.remove(id);
      setSessions((prev) => prev.filter((s) => s.id !== id));
    } catch (err) {
      setError(String(err));
    }
  }

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex items-center gap-2">
        <Swords size={15} className="text-accent" />
        <span className="text-sm font-semibold text-fg">okuro·prism sparring</span>
        <span className="text-xs text-tertiary">the partner that remembers</span>
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-2">
        <input
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && start()}
          placeholder="Topic or decision to spar…"
          className="min-w-[32rem] flex-1 bg-transparent px-1 text-sm text-fg placeholder:text-tertiary focus:outline-none"
        />
        <input
          value={person}
          onChange={(e) => setPerson(e.target.value)}
          placeholder="recipient (person id, optional)"
          className="w-56 bg-transparent px-1 text-sm text-fg placeholder:text-tertiary focus:outline-none"
        />
        <button
          onClick={start}
          disabled={!topic.trim() || starting}
          className="flex items-center gap-1 rounded-md border border-accent/40 px-2.5 py-1 text-xs text-accent hover:bg-accent/10 disabled:opacity-40"
        >
          <Plus size={13} /> {starting ? "opening…" : "Open session"}
        </button>
      </div>

      {error && <p className="text-xs text-[var(--color-status-error,#f92f77)]">{error}</p>}

      {loading ? (
        <p className="text-sm text-tertiary">Loading…</p>
      ) : sessions.length === 0 ? (
        <div className="flex flex-1 items-center justify-center rounded-xl border border-border text-sm text-tertiary">
          No sessions yet — open one above.
        </div>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-3 overflow-y-auto">
          {sessions.map((s) => (
            <div
              key={s.id}
              onClick={() => navigate(`/sparring?id=${encodeURIComponent(s.id)}`)}
              className="group relative flex cursor-pointer flex-col gap-2 rounded-lg border border-border p-3 hover:border-accent/60"
            >
              <div className="flex items-center gap-2">
                <Swords size={14} className="text-tertiary" />
                <span className="truncate text-sm font-medium text-fg">{s.topic}</span>
              </div>
              <div className="flex items-center justify-between text-[11px] text-tertiary">
                <span className="flex items-center gap-1">
                  <Clock size={11} /> {s.turn_count} turns
                </span>
                <span
                  className={
                    s.status === "open"
                      ? "rounded-full border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent"
                      : "rounded-full border border-border px-1.5 py-0.5 text-[10px] text-tertiary"
                  }
                >
                  {s.status}
                </span>
              </div>
              {s.person_id && <div className="text-[11px] text-tertiary">↳ {s.person_id}</div>}
              <button
                onClick={(e) => remove(s.id, e)}
                title="delete session"
                className="absolute right-2 top-2 hidden h-5 w-5 items-center justify-center rounded-full bg-[var(--color-status-error,#f92f77)] text-white group-hover:flex"
              >
                <Trash2 size={11} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// One session — timeline + live board + move bar
// ---------------------------------------------------------------------------

function SessionView({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate();
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const seqRef = useRef(0);

  const refetch = useCallback(async () => {
    try {
      const s = await sparringApi.get(sessionId);
      setSession(s);
    } catch (e) {
      setError(String(e));
    }
  }, [sessionId]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  // change-feed poll — refetch when any process mutates THIS session.
  useEffect(() => {
    const timer = setInterval(async () => {
      try {
        const res = await sparringApi.events(seqRef.current);
        seqRef.current = res.seq;
        if (res.events.some((ev) => ev.session_id === sessionId)) void refetch();
      } catch {
        /* transient — next tick retries */
      }
    }, 3000);
    return () => clearInterval(timer);
  }, [sessionId, refetch]);

  const doMove = useCallback(
    async (move: Move) => {
      setBusy(true);
      setError(null);
      try {
        const s = await sparringApi.move(sessionId, move);
        setSession(s);
      } catch (e) {
        setError(String(e));
      } finally {
        setBusy(false);
      }
    },
    [sessionId],
  );

  if (!session) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-tertiary">
        {error ? <span className="text-[var(--color-status-error,#f92f77)]">{error}</span> : "Loading…"}
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <button
          onClick={() => navigate("/sparring")}
          className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-tertiary hover:text-fg"
        >
          <ArrowLeft size={12} /> Sessions
        </button>
        <Swords size={15} className="text-accent" />
        <span className="truncate text-sm font-semibold text-fg">{session.topic}</span>
        {session.person_id && <span className="text-xs text-tertiary">↳ {session.person_id}</span>}
        <span className="ml-auto text-[11px] text-tertiary">{session.turns.length} turns · {session.status}</span>
      </div>

      <div className="flex min-h-0 flex-1">
        {/* Timeline */}
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex-1 space-y-2 overflow-y-auto p-4">
            {session.turns.length === 0 ? (
              <p className="text-sm text-tertiary">No turns yet. Raise a challenge, surface an assumption, or run the panel.</p>
            ) : (
              session.turns.map((t) => <TurnRow key={t.n} turn={t} onMove={doMove} busy={busy} />)
            )}
          </div>
          <MoveBar onMove={doMove} busy={busy} disabled={session.status !== "open"} />
          {error && <p className="px-4 pb-2 text-xs text-[var(--color-status-error,#f92f77)]">{error}</p>}
        </div>

        {/* Live board */}
        <LiveBoard session={session} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Turn row — kind-styled, with verdict actions on live turns
// ---------------------------------------------------------------------------

const KIND_META: Record<Turn["kind"], { label: string; cls: string; icon: React.ReactNode }> = {
  recall: { label: "recall", cls: "border-[var(--color-accent-subtle,#8884)] text-accent", icon: <Clock size={12} /> },
  challenge: { label: "challenge", cls: "border-[var(--color-status-error,#f92f77)]/40 text-fg", icon: <Zap size={12} /> },
  assumption: { label: "assumption", cls: "border-[var(--color-status-warning,#c52998)]/40 text-fg", icon: <AlertTriangle size={12} /> },
  decision: { label: "decision", cls: "border-accent/40 text-fg", icon: <Check size={12} /> },
  tripwire: { label: "tripwire", cls: "border-border text-fg", icon: <Ban size={12} /> },
  note: { label: "note", cls: "border-border text-tertiary", icon: <span className="text-[10px]">•</span> },
};

const VERDICT_CLS: Record<Verdict, string> = {
  open: "text-tertiary",
  held: "text-accent",
  conceded: "text-[var(--color-status-error,#f92f77)]",
  killed: "text-[var(--color-status-error,#f92f77)]",
  falsified: "text-[var(--color-status-error,#f92f77)]",
};

function TurnRow({ turn, onMove, busy }: { turn: Turn; onMove: (m: Move) => void; busy: boolean }) {
  const meta = KIND_META[turn.kind];
  const sev = (turn.meta?.severity as string) || "";
  const rebuttal = (turn.meta?.rebuttal as string) || "";
  const trigger = (turn.meta?.trigger as string) || "";
  const isOpenChallenge = turn.kind === "challenge" && turn.verdict === "open";
  const isStandingAssumption = turn.kind === "assumption" && (turn.verdict === "open" || turn.verdict === "held");

  function verdict(v: Exclude<Verdict, "open">) {
    onMove({ type: "verdict", ref: turn.n, verdict: v });
  }

  return (
    <div className={`rounded-lg border-l-2 border bg-black/5 p-2.5 ${meta.cls}`}>
      <div className="flex items-center gap-2 text-[11px] text-tertiary">
        <span className="flex items-center gap-1">{meta.icon} {meta.label}</span>
        <span>#{turn.n}</span>
        {sev && <span className="uppercase">· {sev}</span>}
        {turn.source && turn.source !== "user" && <span>· {turn.source}</span>}
        <span className={`ml-auto ${VERDICT_CLS[turn.verdict]}`}>{turn.verdict}</span>
      </div>
      <div className="mt-1 whitespace-pre-line text-sm text-fg">{turn.content}</div>
      {rebuttal && <div className="mt-1 text-xs text-tertiary">steelman: {rebuttal}</div>}
      {trigger && <div className="mt-1 text-xs text-tertiary">fires when: {trigger}</div>}

      {(isOpenChallenge || isStandingAssumption) && (
        <div className="mt-2 flex gap-1.5">
          {isOpenChallenge && (
            <>
              <VerdictBtn label="Rebut" v="held" onClick={verdict} busy={busy} tone="accent" />
              <VerdictBtn label="Concede" v="conceded" onClick={verdict} busy={busy} tone="error" />
              <VerdictBtn label="Kill" v="killed" onClick={verdict} busy={busy} tone="error" />
            </>
          )}
          {isStandingAssumption && (
            <>
              <VerdictBtn label="Holds" v="held" onClick={verdict} busy={busy} tone="accent" />
              <VerdictBtn label="Falsify" v="falsified" onClick={verdict} busy={busy} tone="error" />
            </>
          )}
        </div>
      )}
    </div>
  );
}

function VerdictBtn({
  label,
  v,
  onClick,
  busy,
  tone,
}: {
  label: string;
  v: Exclude<Verdict, "open">;
  onClick: (v: Exclude<Verdict, "open">) => void;
  busy: boolean;
  tone: "accent" | "error";
}) {
  const cls =
    tone === "accent"
      ? "border-accent/40 text-accent hover:bg-accent/10"
      : "border-[var(--color-status-error,#f92f77)]/40 text-[var(--color-status-error,#f92f77)] hover:bg-[var(--color-status-error,#f92f77)]/10";
  return (
    <button
      onClick={() => onClick(v)}
      disabled={busy}
      className={`rounded border px-2 py-0.5 text-[11px] disabled:opacity-40 ${cls}`}
    >
      {label}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Move bar — issue new turns
// ---------------------------------------------------------------------------

const ADD_KINDS: { type: MoveType; label: string }[] = [
  { type: "challenge", label: "Challenge" },
  { type: "assumption", label: "Assumption" },
  { type: "decision", label: "Decision" },
  { type: "tripwire", label: "Tripwire" },
  { type: "note", label: "Note" },
];

function MoveBar({ onMove, busy, disabled }: { onMove: (m: Move) => void; busy: boolean; disabled: boolean }) {
  const [kind, setKind] = useState<MoveType>("challenge");
  const [text, setText] = useState("");
  const [hint, setHint] = useState("");

  function add() {
    const content = text.trim();
    if (!content || busy || disabled) return;
    onMove({ type: kind, content });
    setText("");
  }

  function runPanel() {
    if (busy || disabled) return;
    onMove({ type: "panel", audience_hint: hint.trim() || undefined });
  }

  return (
    <div className="border-t border-border p-2">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as MoveType)}
          disabled={disabled}
          className="rounded-md border border-border bg-transparent px-1.5 py-1 text-xs text-fg focus:outline-none"
        >
          {ADD_KINDS.map((k) => (
            <option key={k.type} value={k.type} className="bg-background text-fg">
              {k.label}
            </option>
          ))}
        </select>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
          placeholder={disabled ? "session closed" : "add a turn…"}
          disabled={disabled}
          className="min-w-[24rem] flex-1 bg-transparent px-1 text-sm text-fg placeholder:text-tertiary focus:outline-none disabled:opacity-50"
        />
        <button
          onClick={add}
          disabled={!text.trim() || busy || disabled}
          className="flex items-center gap-1 rounded-md border border-accent/40 px-2 py-1 text-xs text-accent hover:bg-accent/10 disabled:opacity-40"
        >
          <Plus size={12} /> Add
        </button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input
          value={hint}
          onChange={(e) => setHint(e.target.value)}
          placeholder="panel audience (optional, e.g. a skeptical board)"
          disabled={disabled}
          className="min-w-[24rem] flex-1 bg-transparent px-1 text-xs text-fg placeholder:text-tertiary focus:outline-none disabled:opacity-50"
        />
        <button
          onClick={runPanel}
          disabled={busy || disabled}
          title="fan the position out to a multi-model adversary panel"
          className="flex items-center gap-1 rounded-md border border-[var(--color-status-error,#f92f77)]/40 px-2 py-1 text-xs text-[var(--color-status-error,#f92f77)] hover:bg-[var(--color-status-error,#f92f77)]/10 disabled:opacity-40"
        >
          <Swords size={12} /> {busy ? "sparring…" : "Run adversary panel"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live board — the derived state
// ---------------------------------------------------------------------------

function LiveBoard({ session }: { session: SessionDetail }) {
  const s = session.state;
  return (
    <div className="w-72 shrink-0 space-y-3 overflow-y-auto border-l border-border p-3">
      <div className="text-xs font-semibold uppercase tracking-wide text-tertiary">Live board</div>
      <BoardGroup title="Open challenges" icon={<Zap size={12} />} turns={s.open_challenges} tone="error" empty="none live" />
      <BoardGroup title="Standing assumptions" icon={<AlertTriangle size={12} />} turns={s.standing_assumptions} tone="warn" empty="none" />
      <BoardGroup title="Did NOT hold" icon={<X size={12} />} turns={s.falsified_assumptions} tone="error" empty="—" strike />
      <BoardGroup title="Decisions" icon={<Check size={12} />} turns={s.decisions} tone="accent" empty="none recorded" />
      <BoardGroup title="Tripwires" icon={<Ban size={12} />} turns={s.tripwires} tone="muted" empty="none set" />
    </div>
  );
}

function BoardGroup({
  title,
  icon,
  turns,
  tone,
  empty,
  strike,
}: {
  title: string;
  icon: React.ReactNode;
  turns: Turn[];
  tone: "error" | "warn" | "accent" | "muted";
  empty: string;
  strike?: boolean;
}) {
  const toneCls =
    tone === "error"
      ? "text-[var(--color-status-error,#f92f77)]"
      : tone === "warn"
        ? "text-[var(--color-status-warning,#c52998)]"
        : tone === "accent"
          ? "text-accent"
          : "text-tertiary";
  return (
    <div>
      <div className={`flex items-center gap-1 text-[11px] font-medium ${toneCls}`}>
        {icon} {title} <span className="text-tertiary">· {turns.length}</span>
      </div>
      {turns.length === 0 ? (
        <div className="mt-1 text-[11px] text-tertiary">{empty}</div>
      ) : (
        <ul className="mt-1 space-y-1">
          {turns.map((t) => (
            <li
              key={t.n}
              className={`text-xs text-fg ${strike ? "line-through opacity-70" : ""}`}
              title={t.content}
            >
              • {t.content.length > 70 ? `${t.content.slice(0, 70)}…` : t.content}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
