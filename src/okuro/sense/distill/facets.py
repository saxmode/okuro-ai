### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Read one session out of agent_events the way the distiller must read it — messages, not rows.
# index: imports | EXTRACTION_VERSION | caps | SessionRead | assistant_messages | read_session | render_transcript
# AGENT_HEADER_END -->
"""One session, regrouped from event ROWS into what actually happened.

``agent_events`` is not a list of turns. Claude Code emits **one row per
content block**, so a single assistant message arrives as two, three or four
rows — and every one of them repeats that message's metadata. Measured on the
live store 2026-08-13, on the session with the most assistant rows::

    msg_011Cdgi46v8wdqM8VV   ords 17,18      [text]  [tool_use]
    msg_011Cdgi4muAkEEe9YL   ords 29,30,31,33  [thinking] [text] [tool_use] [tool_use]
    msg_011Cdgi5Mpc4zQoWiA   ords 36,37,38   [thinking] [text] [tool_use]

    60 assistant rows  ->  24 assistant messages

Three consequences, each of which is a defect if the rows are taken at face
value:

**Turn counts triple.** ``COUNT(*) WHERE type='assistant'`` is a block count.
:func:`read_session` counts DISTINCT message ids instead.

**Token mass multiplies.** Every row of a message carries the same
``tokens_in`` / ``tokens_out``, so ``SUM(tokens_out)`` over rows multiplies
output by the blocks-per-message factor. Worse, ``tokens_in`` is the
*cumulative context size* at that message (51123 → 53028 → 69988 across one
session), so summing it is not a quantity of anything at all.
``agent_sessions.tokens_in``/``tokens_out`` are exactly those two sums
(``trace/claude_code.py:317-318``) and are therefore both inflated and, for
the input side, meaningless. This module does not read them. It sums
``tokens_out`` once per distinct message and reports ``context_peak`` as the
maximum ``tokens_in`` the session reached.

**Thinking leaks into the text.** The flat ``agent_events.text`` column is
``_flatten_content``'s concatenation of every block in the row's message —
thinking, text, tool_use arguments, tool_result bodies
(``trace/claude_code.py:61-87``). Feeding that to a facet extractor feeds it
the model's private reasoning and the full text of every file the agent read.
:func:`render_transcript` goes to ``content_json`` and keeps only blocks whose
``type`` is ``"text"``.

The truncation this module defines is the tier-1 contract, so it is written
down rather than left to whoever calls it: **user turns and assistant text
blocks, nothing else.** No tool_result, no tool_use arguments, no thinking, no
system or progress events. Bounded head-and-tail because the opening frames
the task and the ending carries the outcome, and a middle-truncated transcript
loses whichever of the two the cap happens to reach first.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Bump when anything here changes what a facet row would contain. Rows below
# the current version are re-derived; same contract as
# interaction.detect.DETECTOR_VERSION.
EXTRACTION_VERSION = 1

# Per-turn cap. Long turns are pasted logs and file dumps; the discriminating
# signal is at the top. Mirrors interaction.turns.EMBED_CHAR_CAP, which was
# sized against the same corpus.
TURN_CHAR_CAP = 2000

# Total cap on what reaches a model. Held to head + tail so both the framing
# and the outcome survive — a single head-truncation on a long session shows
# the task being set and never shows whether it worked.
TRANSCRIPT_CHAR_CAP = 12_000
_HEAD_SHARE = 0.6

# The event types the transcript is built from. Everything else in
# agent_events (tool_result, system, progress) is deliberately absent.
TRANSCRIPT_TYPES = ("user", "assistant")

_EVENT_COLUMNS = (
    "uuid", "ord", "type", "role", "timestamp",
    "text", "content_json", "tool_name", "tokens_in", "tokens_out",
)


@dataclass
class SessionRead:
    """Everything one pass over a session's events yields.

    Deliberately one object from one query: tier-0 needs the counts, tier-1
    needs the transcript, and reading ``agent_events`` twice for a 10 000
    session corpus is the difference between a pilot that runs and one that
    does not.
    """

    session_id: str
    scanned_through_ord: int = -1
    last_event_ts: str | None = None
    provider: str | None = None

    user_turns: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    distinct_tools: list[str] = field(default_factory=list)
    # None means the provider reports no usage at all (gemini, antigravity),
    # which is not the same as reporting zero.
    tokens_out_total: int | None = None
    # The largest context ONE message carried. claude-code only — codex reports
    # cumulative consumption, which is a different quantity and lives below.
    context_peak: int | None = None
    # Total input consumed across the session. codex only.
    tokens_in_total: int | None = None
    # Which shape the numbers came in: 'per_message' (claude-code),
    # 'cumulative' (codex), or None when there were none.
    token_shape: str | None = None

    # Failed tool calls, and the longest unbroken run of them. The run is the
    # interesting one: scattered errors are ordinary work, whereas five in a
    # row is an agent stuck in a loop, which is one of the sampling policy's
    # flag conditions. Read from the ``is_error`` flag on tool_result blocks —
    # present on 140 of 300 sampled blocks, live store 2026-08-13.
    error_results: int = 0
    max_error_run: int = 0

    # (speaker, text) in ord order — user turns and assistant text blocks only.
    turns: list[tuple[str, str]] = field(default_factory=list)
    # True when at least one event still carries a body. A session the
    # compactor has already nulled reads as a skeleton and any facet derived
    # from it is a weaker claim; distill_facets.bodies_available records that.
    bodies_available: bool = False

    @property
    def total_chars(self) -> int:
        return sum(len(t) for _, t in self.turns)


def _loads(blob: Any) -> dict:
    if not blob:
        return {}
    if isinstance(blob, dict):
        return blob
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text_blocks(content: Any) -> list[str]:
    """The ``type == "text"`` blocks of one message payload, in order.

    A string payload is itself the text (user messages are often plain
    strings). A list payload is Anthropic's content-block array, and only the
    ``text`` members belong in a transcript — ``thinking`` is the model's
    private reasoning and ``tool_use`` / ``tool_result`` are the machine layer
    this truncation exists to exclude.
    """
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    out = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            body = item.get("text") or ""
            if body.strip():
                out.append(body)
    return out


def read_session(db, session_id: str, rows: Iterable[dict] | None = None) -> SessionRead:
    """One pass over a session's events, regrouped by MESSAGE.

    ``rows`` lets a caller that already holds the events (the pilot batches
    them) avoid a second query; the grouping logic is identical either way, so
    there is one implementation of the parity rule rather than two.
    """
    if rows is None:
        cols = ", ".join(_EVENT_COLUMNS)
        rows = db.fetchall(
            f"SELECT {cols} FROM agent_events WHERE session_id = ? ORDER BY ord",
            (session_id,),
        )

    read = SessionRead(session_id=session_id)

    # message id -> tokens_out, so a message contributes its output once no
    # matter how many blocks it was split across.
    tokens_by_message: dict[str, int] = {}
    seen_messages: set[str] = set()
    tools: list[str] = []
    error_run = 0
    # The codex shape: running totals on `progress` rows. Collected separately
    # because summing them is meaningless and only their maximum is a quantity.
    cumulative_out = 0
    cumulative_in = 0
    saw_cumulative = False

    for row in rows:
        ord_ = row["ord"] if row["ord"] is not None else -1
        if ord_ > read.scanned_through_ord:
            read.scanned_through_ord = ord_
        ts = row["timestamp"]
        if ts and (read.last_event_ts is None or ts > read.last_event_ts):
            read.last_event_ts = ts

        etype = row["type"]
        payload = _loads(row["content_json"])
        if row["content_json"] or row["text"]:
            read.bodies_available = True

        # A tool CALL is a tool_use block on the assistant side. Counting
        # tool_result rows instead would count the same call again from the
        # other end, and would miss a call whose result never came back.
        #
        # DEPENDS ON: the ingester tagging only the assistant's tool_use row.
        # `_flatten_content` (trace/claude_code.py:69-76) returns a name for a
        # `tool_use` item and for nothing else, so the answering tool_result
        # row carries NULL — measured live 2026-08-13 over the whole corpus:
        # 340,225 of 606,672 assistant rows named a tool, and 0 of 340,728
        # tool_result rows did. If that ever changes every count here doubles;
        # tests/sense/distill/test_message_parity.py pins it.
        if row["tool_name"]:
            read.tool_calls += 1
            if row["tool_name"] not in tools:
                tools.append(row["tool_name"])

        if etype == "assistant":
            # The message id is what makes rows into turns. Absent (a provider
            # that does not emit one), the row's own uuid stands in — that
            # degrades to one-message-per-row, which is the pre-grouping
            # behaviour and never worse than it.
            mid = payload.get("id") or row["uuid"]
            if mid not in seen_messages:
                seen_messages.add(mid)
                read.assistant_messages += 1
                out = row["tokens_out"]
                if out:
                    tokens_by_message[mid] = int(out)
            tin = row["tokens_in"]
            if tin:
                cumulative_in = max(cumulative_in, int(tin))
            for block in _text_blocks(payload.get("content")):
                read.turns.append(("assistant", block[:TURN_CHAR_CAP]))

        elif etype == "user":
            read.user_turns += 1
            # Strip the harness furniture — system-reminders, IDE notices,
            # resume caveats. Reusing interaction.turns keeps ONE definition
            # of "what the sender actually wrote" for the whole store.
            from okuro.sense.interaction.turns import clean_turn_text

            body = clean_turn_text(row["text"])
            if not body:
                # A user row whose text is empty may still carry structured
                # content (or have been compacted). Fall back to the blocks.
                body = "\n".join(_text_blocks(payload.get("content")))
            if body.strip():
                read.turns.append(("user", body[:TURN_CHAR_CAP]))

        # THE CODEX SHAPE. codex puts usage on `progress` rows and the figures
        # are RUNNING TOTALS, duplicated across rows — measured live
        # 2026-08-13: 1535 monotonically non-decreasing values on session
        # 019c4a4f, max 389 244 against a sum of 305 182 044. Only the maximum
        # is a quantity; the sum is the running total added to itself.
        elif etype == "progress" and (row["tokens_out"] or row["tokens_in"]):
            saw_cumulative = True
            if row["tokens_out"]:
                cumulative_out = max(cumulative_out, int(row["tokens_out"]))
            if row["tokens_in"]:
                cumulative_in = max(cumulative_in, int(row["tokens_in"]))

        # tool_result / system / progress: counted above if they name a tool,
        # never rendered. This is the truncation contract.
        #
        # The one thing read out of a tool_result is whether it failed. The
        # body stays excluded; only the flag is taken.
        if etype == "tool_result":
            failed = any(
                isinstance(item, dict) and item.get("is_error")
                for item in (payload.get("content") or [])
                if isinstance(payload.get("content"), list)
            )
            if failed:
                read.error_results += 1
                error_run += 1
                if error_run > read.max_error_run:
                    read.max_error_run = error_run
            else:
                error_run = 0

    # Resolve the two provider shapes into one honest pair.
    #
    # claude-code (9197 sessions) puts per-message usage on assistant rows: the
    # output figures are NOT cumulative (measured: 148, 174, 384, 2234, 915, 72
    # across consecutive messages), so they sum — once per DISTINCT message id.
    #
    # codex (541 sessions) puts running totals on progress rows, so only the
    # maximum means anything.
    #
    # gemini (167) and antigravity (105) carry usage on no row type at all, and
    # those sessions end with None — unknown, which is not zero.
    # The two input figures are NOT the same quantity and do not share a
    # column. codex's tokens_in is a running total of input CONSUMED; reading
    # its maximum as a context size produced a session claiming a
    # 108 642 435-token context.
    if tokens_by_message:
        read.tokens_out_total = sum(tokens_by_message.values())
        read.token_shape = "per_message"
        read.context_peak = cumulative_in or None
    elif saw_cumulative:
        read.tokens_out_total = cumulative_out
        read.token_shape = "cumulative"
        read.tokens_in_total = cumulative_in or None

    read.distinct_tools = tools
    return read


def render_transcript(read: SessionRead, cap: int = TRANSCRIPT_CHAR_CAP) -> str:
    """The bounded text a facet extractor sees. Deterministic.

    Head and tail rather than a single truncation: on a long session the head
    alone shows a task being set and never shows whether it was done, and the
    tail alone shows an outcome with nothing to judge it against.
    """
    if not read.turns:
        return ""
    parts = [f"{speaker}: {body}" for speaker, body in read.turns]
    whole = "\n\n".join(parts)
    if len(whole) <= cap:
        return whole
    head = int(cap * _HEAD_SHARE)
    tail = cap - head
    return f"{whole[:head]}\n\n[... {len(whole) - cap} chars elided ...]\n\n{whole[-tail:]}"
