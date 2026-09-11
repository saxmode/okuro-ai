/**
 * Pure reducer that folds an SSE event stream into the rendered
 * ``InlineSessionView`` state.
 *
 * Pulled out of ``use-session-stream`` so it can be unit-tested without
 * spinning up an EventSource. The hook owns the network; this file
 * owns the shape transitions.
 *
 * Contract behaviours preserved here:
 *
 *   1. ``token`` deltas append into the latest assistant message that
 *      matches ``message_id``. A token arriving before ``message_start``
 *      is tolerated — we materialise an empty assistant bubble on the fly.
 *   2. ``message_stop`` flips ``streaming=false`` and replaces ``text``
 *      with the authoritative ``text`` field (handles servers that send
 *      a final body without a token stream — gemini/codex case).
 *   3. ``tool_call_start`` → card in ``running``; tier===read stays
 *      ``running`` until ``tool_call_done`` arrives with ``outcome=auto``.
 *   4. ``tool_approval_required`` flips the card to ``pending-approval``
 *      and records the expiry timestamp.
 *   5. ``tool_call_done`` resolves the card state from ``outcome``.
 *   6. Tool calls are anchored to the most-recent assistant message; if
 *      none exists yet (rare — adapter quirk), they live unanchored and
 *      the right column still renders them.
 *   7. ``last_seq`` only advances; out-of-order replay events are
 *      tolerated (the bus may re-emit during reconnect).
 */
import type {
  InlineEvent,
  InlineSessionView,
  ToolCard,
  TranscriptMessage,
} from "@/types/inline";

export const initialSessionView: InlineSessionView = {
  last_seq: 0,
  state: "running",
  messages: [],
  tool_cards: [],
  usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0 },
  connected: false,
};

function upsertMessage(
  messages: TranscriptMessage[],
  message_id: string,
  role: "assistant" | "user",
  ts: string,
): { list: TranscriptMessage[]; idx: number } {
  const idx = messages.findIndex((m) => m.message_id === message_id);
  if (idx >= 0) return { list: messages, idx };
  const next: TranscriptMessage = {
    message_id,
    role,
    text: "",
    streaming: role === "assistant",
    tool_call_ids: [],
    ts,
  };
  return { list: [...messages, next], idx: messages.length };
}

function anchorToolToLatestAssistant(
  messages: TranscriptMessage[],
  call_id: string,
): TranscriptMessage[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m || m.role !== "assistant") continue;
    if (m.tool_call_ids.includes(call_id)) return messages;
    const next = [...messages];
    next[i] = { ...m, tool_call_ids: [...m.tool_call_ids, call_id] };
    return next;
  }
  return messages;
}

function patchCard(
  cards: ToolCard[],
  call_id: string,
  patch: Partial<ToolCard>,
): ToolCard[] {
  const idx = cards.findIndex((c) => c.call_id === call_id);
  if (idx < 0) return cards;
  const target = cards[idx];
  if (!target) return cards;
  const next = [...cards];
  next[idx] = { ...target, ...patch };
  return next;
}

/**
 * Fold one event into the view. Returns a NEW object only when
 * something changed; identity-preserving on unknown event types so
 * React renders skip the no-op.
 */
export function applyEvent(
  state: InlineSessionView,
  event: InlineEvent,
): InlineSessionView {
  // Out-of-order replay tolerance — we still apply, but only advance
  // last_seq forward. (The transcript will end up correctly ordered
  // because the server replay phase delivers events in seq order; this
  // is belt-and-braces for bus jitter.)
  const next_last_seq =
    typeof event.seq === "number" && event.seq > state.last_seq
      ? event.seq
      : state.last_seq;

  switch (event.type) {
    case "status": {
      return {
        ...state,
        last_seq: next_last_seq,
        state: event.state,
        state_detail:
          event.detail == null
            ? undefined
            : typeof event.detail === "string"
              ? event.detail
              : JSON.stringify(event.detail),
      };
    }

    case "message_start": {
      const { list } = upsertMessage(
        state.messages,
        event.message_id,
        event.role,
        event.ts,
      );
      return { ...state, last_seq: next_last_seq, messages: list };
    }

    case "token": {
      const { list, idx } = upsertMessage(
        state.messages,
        event.message_id,
        "assistant",
        event.ts,
      );
      const target = list[idx];
      if (!target) return state;
      const replaced = [...list];
      replaced[idx] = {
        ...target,
        text: target.text + event.delta,
        streaming: true,
      };
      return { ...state, last_seq: next_last_seq, messages: replaced };
    }

    case "message_stop": {
      const { list, idx } = upsertMessage(
        state.messages,
        event.message_id,
        "assistant",
        event.ts,
      );
      const target = list[idx];
      if (!target) return state;
      const replaced = [...list];
      replaced[idx] = {
        ...target,
        // Authoritative body — prefer event.text if non-empty, otherwise
        // keep whatever the token stream already accumulated. Some
        // adapters send empty text on stop (claude when interrupted).
        text: event.text || target.text,
        streaming: false,
      };
      return { ...state, last_seq: next_last_seq, messages: replaced };
    }

    case "user_echo": {
      // The user-input POST already optimistically inserted the bubble
      // (see ``optimisticUserMessage`` in the hook). Backfill any text
      // drift from the adapter (claude can normalise whitespace).
      const { list, idx } = upsertMessage(
        state.messages,
        event.message_id,
        "user",
        event.ts,
      );
      const target = list[idx];
      if (!target) return state;
      const replaced = [...list];
      replaced[idx] = { ...target, text: event.text, streaming: false };
      return { ...state, last_seq: next_last_seq, messages: replaced };
    }

    case "tool_call_start": {
      const card: ToolCard = {
        call_id: event.call_id,
        tool_name: event.tool_name,
        tier: event.tier,
        args: event.args,
        state: "running",
        approval_required: false,
        started_at: event.ts,
      };
      // Idempotent — repeated start events (server replay) reuse the row.
      const existing = state.tool_cards.findIndex(
        (c) => c.call_id === event.call_id,
      );
      const tool_cards =
        existing >= 0
          ? state.tool_cards
          : [...state.tool_cards, card];
      const messages = anchorToolToLatestAssistant(state.messages, event.call_id);
      return { ...state, last_seq: next_last_seq, tool_cards, messages };
    }

    case "tool_approval_required": {
      // The tool_call_start may not have arrived yet on a fresh replay —
      // create the card on the fly so the user can act.
      let cards = state.tool_cards;
      if (!cards.some((c) => c.call_id === event.call_id)) {
        cards = [
          ...cards,
          {
            call_id: event.call_id,
            tool_name: event.tool_name,
            tier: event.tier,
            args: event.args,
            state: "pending-approval",
            approval_required: true,
            approval_expires_at: event.expires_at,
            started_at: event.ts,
          },
        ];
      } else {
        cards = patchCard(cards, event.call_id, {
          state: "pending-approval",
          approval_required: true,
          approval_expires_at: event.expires_at,
          args: event.args,
        });
      }
      const messages = anchorToolToLatestAssistant(state.messages, event.call_id);
      return { ...state, last_seq: next_last_seq, tool_cards: cards, messages };
    }

    case "tool_call_done": {
      const cardState =
        event.outcome === "denied" || event.outcome === "timeout"
          ? "denied"
          : event.outcome === "error"
            ? "error"
            : "done";
      const tool_cards = patchCard(state.tool_cards, event.call_id, {
        state: cardState,
        outcome: event.outcome,
        result: event.result,
        error: event.error,
        duration_ms: event.duration_ms,
        approval_required: false,
      });
      return { ...state, last_seq: next_last_seq, tool_cards };
    }

    case "usage": {
      return {
        ...state,
        last_seq: next_last_seq,
        usage: {
          input_tokens: event.input_tokens ?? state.usage.input_tokens,
          output_tokens: event.output_tokens ?? state.usage.output_tokens,
          cost_usd: event.cost_usd ?? state.usage.cost_usd,
        },
      };
    }

    case "error": {
      return {
        ...state,
        last_seq: next_last_seq,
        last_error: { code: event.code, message: event.message },
        // An unrecoverable error implies the session is dying — surface
        // the state flip even if the server hasn't sent ``status:error``
        // yet (race window during shutdown).
        state: event.recoverable === false ? "error" : state.state,
      };
    }

    default:
      return state;
  }
}

/**
 * Insert a user message before the SSE round-trip resolves. The
 * server will later send ``user_echo`` which reconciles the text.
 */
export function optimisticUserMessage(
  state: InlineSessionView,
  message_id: string,
  text: string,
): InlineSessionView {
  // If the bubble already exists (echo arrived first — rare), no-op.
  if (state.messages.some((m) => m.message_id === message_id)) return state;
  return {
    ...state,
    messages: [
      ...state.messages,
      {
        message_id,
        role: "user",
        text,
        streaming: false,
        tool_call_ids: [],
        ts: new Date().toISOString(),
      },
    ],
  };
}
