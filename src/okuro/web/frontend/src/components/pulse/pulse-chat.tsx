import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowRight, Mic } from "lucide-react";
import { chatApi, type ChatTurn } from "@/lib/api";
import { useDictation } from "@/lib/dictation";
import { snapshotPageContext, dispatchChatAction } from "@/lib/page-context";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { ActionForm } from "./action-form";
import { cn } from "@/lib/utils";

/**
 * PulseChat — context-aware general chat. Rendered by Sidebar as a
 * full-height overlay BEHIND the pulse orb (the orb floats on top; the
 * Sidebar wrapper owns positioning + fade). Input docks at the bottom.
 *
 * Typography: one monospace size for everything (CLI feel). Speakers are
 * distinguished by ALIGNMENT — the user is right-aligned, okuro left —
 * not by colour blocks or bubbles. No backgrounds anywhere.
 *
 * Streams NDJSON activity (bootstrap → reading → calling … → answer) from
 * ``/api/chat`` via ``chatApi.send``, grounded in okuro memory server-side.
 * Assistant replies render as markdown (tables, code, lists) normalised to
 * the same size + mono family. ESC-to-close is handled by AppShell.
 */
type LogKind = "me" | "dim" | "ok" | "err" | "form";
interface LogEntry {
  kind: LogKind;
  text: string;
  form?: { name: string; context: string };
}

// Draw-intent fast path. A "draw / visualize … flow" command shouldn't pay a
// full chat-LLM round-trip just to be ROUTED to the flow view — that routing
// inference was ~30-40s of dead time before drawing even began. Detect the
// intent client-side and fire the draw action immediately (navigate /flow +
// stream the graph), skipping /api/chat entirely. Anything else still goes to
// the chat agent, which can also emit a draw action for fuzzier phrasings.
const _DRAW_VERB = /\b(draw|visuali[sz]e|sketch|diagram|graph|flow ?chart|map out)\b/i;
const _FLOW_HINT = /\b(okuro[.\s]?flow|flow ?chart|flow ?diagram|flow ?graph|flow canvas|\bflow\b)\b/i;
function isDrawCommand(p: string): boolean {
  if (!_DRAW_VERB.test(p)) return false;
  // A draw verb plus a flow reference, OR the message simply opens with the
  // verb (e.g. "draw the login sequence") — both are unambiguous draw commands.
  return _FLOW_HINT.test(p) || _DRAW_VERB.test(p.trimStart().slice(0, 24));
}

// okuro's replies: one size, one family, FULL ACCENT colour — markdown
// descendants are forced to it so tables/headings/code read like terminal
// output in the accent tone, not a document.
const MD_OKURO =
  "font-mono text-[15px] leading-relaxed text-accent " +
  "[&_*]:!font-mono [&_*]:!text-[15px] [&_*]:!font-normal [&_*]:!text-accent " +
  "[&_strong]:!font-bold [&_h1]:!font-bold [&_h2]:!font-bold [&_h3]:!font-bold " +
  "[&_p]:my-1 [&_ul]:my-1 [&_ol]:my-1 [&_h1]:my-1 [&_h2]:my-1 [&_h3]:my-1 " +
  "[&_pre]:!bg-transparent [&_pre]:!border-0 [&_pre]:!p-0 [&_pre]:my-1 " +
  "[&_code]:!bg-transparent [&_code]:!border-0 [&_code]:!p-0 " +
  "[&_table]:!my-1";

export function PulseChat({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState<LogEntry[]>([]);
  const historyRef = useRef<ChatTurn[]>([]);
  const logRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  // Stable per-mount id so the backend reuses ONE persistent CLI session for
  // this conversation (no per-turn spawn). Fresh on each page load.
  const conversationId = useRef(
    (globalThis.crypto?.randomUUID?.() ?? `c-${Date.now()}-${Math.round(Math.random() * 1e9)}`),
  ).current;

  void onClose; // close handled via ESC (AppShell) + orb; no in-panel button

  const push = useCallback(
    (kind: LogKind, text: string) =>
      setLog((l) => [...l.slice(-199), { kind, text }]),
    [],
  );

  const pushForm = useCallback(
    (name: string, context: string) =>
      setLog((l) => [...l.slice(-199), { kind: "form" as LogKind, text: "", form: { name, context } }]),
    [],
  );

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log, open]);

  useEffect(() => {
    if (open) {
      const t = setTimeout(() => taRef.current?.focus(), 360);
      return () => clearTimeout(t);
    }
  }, [open]);

  // Auto-grow the input with content (up to a cap, then scroll). Runs on every
  // value change — typing, paste, dictation. Reset to auto first so it shrinks
  // back when text is deleted.
  const MAX_INPUT_H = 200;
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_INPUT_H)}px`;
  }, [prompt]);

  const { recording, toggle } = useDictation((t) =>
    setPrompt((p) => (p ? p + " " : "") + t),
  );

  const send = useCallback(async () => {
    const p = prompt.trim();
    if (!p || busy) return;
    push("me", p);
    historyRef.current = [...historyRef.current, { role: "user", text: p }];
    setPrompt("");

    // Fast path: a draw command jumps straight to the flow canvas + streams,
    // no chat-routing inference. The draw handler (registered by
    // ChatCapabilities) navigates to /flow and renders node-by-node.
    if (isDrawCommand(p)) {
      const ack = "Drawing it on the okuro·flow canvas — opening it now.";
      push("ok", ack);
      historyRef.current = [...historyRef.current, { role: "assistant", text: ack }];
      void dispatchChatAction({ name: "draw", target: p }, (m) => push("dim", m))
        .then((ok) => {
          if (!ok) push("err", "couldn't open the flow canvas");
        })
        .catch((e) => push("err", (e as Error).message || String(e)));
      return;
    }

    setBusy(true);
    try {
      const r = await chatApi.send(
        {
          prompt: p,
          history: historyRef.current,
          page: snapshotPageContext(),
          conversation_id: conversationId,
        },
        {
          onActivity: (msg) => push("dim", msg),
          onAction: (action) => {
            // Fire-and-forget: registered handlers (e.g. flow draw) are async
            // and stream their own activity; the chat stream finishes
            // independently. Built-in scroll/highlight resolve instantly.
            void dispatchChatAction(action, (m) => push("dim", m))
              .then((ok) => {
                if (!ok)
                  push("dim", `couldn't ${action.name} → ${action.target}`);
              })
              .catch((e) => push("err", (e as Error).message || String(e)));
          },
          onForm: (f) => pushForm(f.name, f.context),
        },
      );
      push("ok", r.text || "(no answer)");
      historyRef.current = [
        ...historyRef.current,
        { role: "assistant", text: r.text },
      ];
    } catch (e) {
      push("err", (e as Error).message || String(e));
    } finally {
      setBusy(false);
    }
  }, [prompt, busy, push, pushForm]);

  return (
    // `absolute inset-0` (not h-full): WebKitGTK doesn't resolve height:100%
    // through the absolutely-positioned Sidebar overlay, which collapsed the
    // log to content height. inset-0 gives a definite box so flex-1 fills.
    <div className="absolute inset-0 flex flex-col px-3 pb-3 pt-2" aria-hidden={!open}>
      {/* conversation — hugs the bottom so messages clear the orb above. */}
      <div ref={logRef} className="min-h-0 flex-1 overflow-y-auto pr-1">
        <div className="flex min-h-full flex-col justify-end gap-3 font-mono text-[15px] leading-relaxed">
          {log.length === 0 ? (
            <span className="self-start text-tertiary">ask okuro anything…</span>
          ) : (
            log.map((m, i) =>
              m.kind === "form" && m.form ? (
                <ActionForm key={i} name={m.form.name} context={m.form.context} onActivity={(msg) => push("dim", msg)} />
              ) : m.kind === "ok" ? (
                <MarkdownContent
                  key={i}
                  variant="compact"
                  className={cn("max-w-[95%] self-start break-words", MD_OKURO)}
                >
                  {m.text}
                </MarkdownContent>
              ) : m.kind === "dim" ? (
                // state / action lines — smaller + 40% opacity
                <p
                  key={i}
                  className="max-w-[90%] self-start whitespace-pre-wrap break-words text-[11px] leading-snug text-fg opacity-40"
                >
                  {m.text}
                </p>
              ) : (
                <p
                  key={i}
                  className={cn(
                    "max-w-[90%] whitespace-pre-wrap break-words",
                    // user → foreground colour, right-aligned
                    m.kind === "me" && "self-end text-right text-fg",
                    m.kind === "err" &&
                      "self-start text-[var(--color-status-error,#f92f77)]",
                  )}
                >
                  {m.text}
                </p>
              ),
            )
          )}
        </div>
      </div>

      {/* input — bracketed by a line top and bottom. */}
      <div className="mt-2 flex shrink-0 items-end gap-2 border-y border-border py-2">
        <textarea
          ref={taRef}
          rows={1}
          value={prompt}
          disabled={busy}
          placeholder={busy ? "thinking…" : "message okuro…"}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          className="max-h-[200px] min-h-8 flex-1 resize-none overflow-y-auto bg-transparent font-mono text-[15px] text-fg placeholder:text-tertiary focus:outline-none"
        />
        <button
          onClick={send}
          disabled={busy || !prompt.trim()}
          aria-label="send"
          className="shrink-0 rounded p-1 text-tertiary transition-colors hover:text-accent disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
        >
          <ArrowRight className="size-4" />
        </button>
        <button
          onClick={toggle}
          aria-label={recording ? "stop dictation" : "dictate"}
          className={cn(
            "shrink-0 rounded p-1 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
            recording
              ? "text-[var(--color-status-error,#f92f77)]"
              : "text-tertiary hover:text-accent",
          )}
        >
          <Mic className="size-4" />
        </button>
      </div>
    </div>
  );
}
