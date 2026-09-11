# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: LLM extraction pass — one note in, typed+tagged items out.
# index:
#   class ExtractedItem
#   def _normalize
#   def item_key
#   def verified_span
#   def build_prompt
#   def _parse
#   def _coerce_item
#   def extract_items
# AGENT_HEADER_END -->
"""LLM extraction pass over a single okuro note.

The note body is prose (meeting notes, half-thoughts, drawings-with-captions),
so heuristics only ever reached the ``- [ ]`` checkboxes — a small fraction of
what a note actually commits the owner to. This module sends the body to the
bridge and gets back typed items, each carrying the tag block the Inbox needs
to explain itself: what it is about (topic/entities), where it came from
(context), and why it earned a slot on the list (rationale).

Routing lives in :mod:`okuro.sense.notes_extract.engine` — this module only
decides *what the note says*, never *where it lands*.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Notes autosave on every keystroke, so half of them are two words long. Below
# this the LLM has nothing to reason about and the call is pure cost.
MIN_BODY_LEN = 80

# Bodies are truncated before the call. The cap was 6000 with the rationale
# "a note past this is a document, and the actionable content is reliably near
# the top" — an ASSERTION about where content lives, not a downstream limit.
# Nothing constrained it: no context window, no column width, no API cap. It
# was the first-cut default from the commit that shipped this module.
#
# Measured 2026-07-29 over 494 non-archived notes:
#   p50 1,491 · p75 8,266 · p90 18,031 · p95 39,685 · max 1,861,462 chars
#   cap  6,000 -> 148 notes truncated (30.0%)
#   cap 50,000 ->  22 notes truncated (4.5%)
# Meeting notes grow DOWNWARD, so on a truncated note the newest paragraph —
# the one a live trigger exists to react to — was never sent. 50k covers 95.5%
# of notes whole. Latency does not pay for it: measured, extraction time tracks
# OUTPUT item count, not input length (a larger input ran faster).
#
# A cap is still required — the largest note is 1.86M chars — so this moves the
# problem to the tail rather than removing it. What must NOT come back is the
# position assumption: whatever handles the remaining 4.5% has to keep the end
# of the note, never blindly the start. See TRUNCATION_MARKER below.
MAX_BODY_CHARS = 50_000

# Truncation is now VISIBLE to both the model and the operator. The 6000 cap
# survived a year because dropping text made no sound: the extractor returned
# fewer items and looked like it had simply found less. A silent lossy step is
# indistinguishable from a working one, which is why this is a marker and a log
# rather than a bare slice.
TRUNCATION_MARKER = "\n\n[...{dropped:,} characters truncated — this note continues beyond what was sent...]"

_VALID_KINDS = ("todo", "signal", "idea", "question")
_VALID_SEVERITIES = ("info", "warn", "crit")

_SYSTEM_PROMPT = (
    "You extract actionable items from a personal knowledge worker's notes. "
    "You are precise, you never invent commitments that are not in the text, "
    "and you return JSON only — no prose, no code fences."
)

_PROMPT_TEMPLATE = """\
Read this note and extract every item worth surfacing on a work dashboard.

<note>
<title>{title}</title>
<project>{project}</project>
<body>
{body}
</body>
</note>

Return JSON: {{"items": [...]}}. Each item:

  kind       one of: todo | signal | idea | question
  text       the item as a single imperative line (max 100 chars)
  source_span the VERBATIM run of words from <body> that this item comes from —
              copied character-for-character, not paraphrased, not summarised,
              not re-punctuated (max 200 chars). This is the item's identity
              across runs: `text` is your wording and may differ next time,
              `source_span` is the user's wording and must not. If the item
              comes from several lines, quote the single most specific one.
  topic      a short kebab-case subject tag, e.g. "meridian-strategy", "deck-review"
  entities   array of named people/projects/systems the item involves (may be empty)
  context    where in the note this came from — quote or name the section (max 120 chars)
  rationale  why this deserves a slot on the user's list (max 140 chars)
  confidence 0.0-1.0 — how sure you are this is a real, intended item
  priority   REQUIRED for kind=todo, omit otherwise. HIGHER = MORE URGENT:
               5 = critical — a named deadline, or someone is blocked on it
               4 = high — committed to a person, no date yet
               3 = normal
               2 = low
               1 = someday / nice-to-have
  severity   info | warn | crit — REQUIRED for kind=signal, omit otherwise

How to choose `kind`:

  todo      A concrete action with a plausible owner. Includes BOTH explicit
            checkboxes ("- [ ] send the deck") AND commitments stated in prose
            ("I'll get back to Ruth on this", "we need to fix the embed pin").
            If someone is on the hook for doing a specific thing, it is a todo.
  signal    An observation, risk, or state-of-the-world remark that needs human
            judgment before it becomes work. "Meridian feels scattered",
            "nobody owns the migration". Not yet an action.
  question  An open question or pending decision, incl. ones owed by others.
            "Does Alex approve GCP tenancy?"
  idea      A speculative "what if" — no commitment, no decision needed yet.

Rules:

  - Extract nothing that is not in the note. No inferred next steps, no
    "they should probably also...". If the note is a pure record with no
    forward motion, return {{"items": []}}.
  - Ignore personal/private life content entirely (health, family, shopping,
    appointments) — it does not belong on a work dashboard.
  - Merge duplicates: if the note says the same thing twice, emit it once.
  - `rationale` must justify SURFACING, not restate the text.
      good: "committed to Ruth in a meeting, no date set, blocks the Q3 deck"
      bad:  "because the note says to send the deck"
  - Prefer fewer, higher-confidence items. Under 8 per note.

JSON only."""


@dataclass
class ExtractedItem:
    """One typed, tagged item lifted out of a note body."""

    kind: str
    text: str
    # Verbatim quote from the note body this item was lifted from. The item's
    # stable identity — see :func:`item_key`. Empty when the model omitted it
    # or quoted something that is not actually in the note.
    source_span: str = ""
    topic: str = ""
    entities: list[str] = field(default_factory=list)
    context: str = ""
    rationale: str = ""
    confidence: float = 0.5
    # okuro's canonical todo scale: 1 (low) .. 5 (critical) — see
    # sense/todos.py::todo_add. Higher is more urgent, and inbox ranking reads
    # it as importance = priority/5 (inbox/scorer.py::priority_to_importance).
    # The first cut of the prompt inverted this ("1 (urgent) .. 5 (someday)"),
    # so the most urgent item okuro found — "Present SmartSend to sales team
    # tomorrow" — scored importance 0.2, the lowest in the system, and never
    # surfaced. Migration 097 repaired the rows it wrote.
    priority: int = 3
    severity: str = "info"


def _normalize(text: str) -> str:
    """Collapse an item's text to a comparison key.

    Lowercased, alphanumerics-and-spaces only, whitespace collapsed. Absorbs
    the churn that does NOT change intent — punctuation, capitalisation, an
    added trailing period — so a note edited elsewhere on the page does not
    re-mint items the user already actioned.

    Deliberately does NOT absorb rewording: if the LLM returns "send Ruth the
    deck" where it previously said "email Ruth the deck", that is a new key and
    a new item. Fuzzy matching here would silently swallow genuine edits, which
    is the worse failure — a duplicate is visible and dismissable, a swallowed
    item is not.
    """
    cleaned = re.sub(r"[^a-z0-9\s]", "", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


# A span shorter than this is not evidence of anything — "fix it", "call him"
# occur all over a note body and would collapse distinct commitments.
_MIN_SPAN_LEN = 16


def item_key(note_id: str, text: str,
             source_span: str = "", body: str = "") -> str:
    """Stable identity for an extracted item — the ledger's UNIQUE key.

    Anchored to the NOTE's words, not the model's. ``text`` is the LLM's
    rendering of the item and is not reproducible: re-running the same
    unchanged note yields "Arrange for Milo to start in two weeks" one day and
    "Coordinate Milo's start in two weeks given canceled flight" the next.
    Keying on that made "a re-extraction of the same text stays idempotent"
    true only when the model happened to pick the same words — so every
    re-extraction minted a fresh key and a fresh todo. Measured 2026-07-25: 5
    duplicate pairs, every one the same note re-extracted with a paraphrase,
    two of them 30 minutes apart.

    ``source_span`` is the verbatim quote the model was asked to copy out of
    the body, and the body does not change between runs — so it keys stably.
    It is trusted ONLY if it is really in the note (normalised substring) and
    long enough to identify something; a hallucinated or trivial span falls
    back to the legacy text key rather than colliding two real commitments.
    This is exact matching on verified source text, not fuzzy merging — the
    reasoning in :func:`_normalize` still stands: a swallowed item is worse
    than a visible duplicate, so nothing here merges on resemblance.
    """
    span = verified_span(source_span, body)
    basis = span or _normalize(text)
    return hashlib.sha256(f"{note_id}:{basis}".encode()).hexdigest()[:16]


def verified_span(source_span: str, body: str) -> str:
    """The normalised span if it is trustworthy, else "".

    Trustworthy = really present in the note (normalised substring) and long
    enough to identify something. One definition, two callers: :func:`item_key`
    decides what to hash, and the engine decides whether a cross-run span
    comparison is valid at all. Splitting the test between them would let the
    two drift into disagreeing about which items are anchored.
    """
    span = _normalize(source_span)
    if len(span) >= _MIN_SPAN_LEN and span in _normalize(body):
        return span
    return ""


def clip_body(body: str) -> tuple[str, int]:
    """Cap the body for the prompt. Returns (clipped, chars_dropped).

    Separate from :func:`build_prompt` so the caller can SEE that text was
    dropped and say so. The old inline ``body[:MAX_BODY_CHARS]`` gave nobody
    that chance — an extraction that silently ignored two thirds of a note was
    indistinguishable from one that found less to extract.
    """
    if len(body) <= MAX_BODY_CHARS:
        return body, 0
    dropped = len(body) - MAX_BODY_CHARS
    return body[:MAX_BODY_CHARS] + TRUNCATION_MARKER.format(dropped=dropped), dropped


def build_prompt(title: str, body: str, project: str | None) -> str:
    clipped, dropped = clip_body(body)
    if dropped:
        # WARN, not DEBUG: this is data loss, and the whole reason the previous
        # cap went unnoticed for a year is that it produced no signal anywhere.
        log.warning(
            "notes_extract: note truncated for extraction — %s of %s chars sent, "
            "%s dropped from the END (title=%r). Items in the dropped region "
            "will not be extracted.",
            f"{MAX_BODY_CHARS:,}", f"{len(body):,}", f"{dropped:,}",
            (title or "Untitled")[:60],
        )
    return _PROMPT_TEMPLATE.format(
        title=(title or "Untitled").strip(),
        project=(project or "none").strip(),
        body=clipped,
    )


def _parse(text: str) -> dict:
    """Extract a JSON object from LLM output, tolerant of surrounding prose.

    ``response_format="json"`` is honoured by local-http providers only — CLI
    providers (claude/codex/gemini) ignore it and answer in text, so the
    fenced/chatty cases are the norm rather than the exception. Mirrors the
    tolerant parse in ai_models/prompt_research.py.
    """
    if not text:
        return {}
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


def _coerce_item(raw: Any) -> ExtractedItem | None:
    """Validate one LLM item dict into an ExtractedItem, or drop it.

    Defensive by design: the model is instructed to emit a closed vocabulary
    but is not schema-bound on the CLI path, so every field is re-checked here
    rather than trusted into a CHECK-constrained INSERT downstream.
    """
    if not isinstance(raw, dict):
        return None

    kind = str(raw.get("kind", "")).strip().lower()
    text = str(raw.get("text", "")).strip()
    if kind not in _VALID_KINDS or len(text) < 8:
        return None

    try:
        confidence = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5

    try:
        priority = int(raw.get("priority", 3))
    except (TypeError, ValueError):
        priority = 3

    severity = str(raw.get("severity", "info")).strip().lower()
    if severity not in _VALID_SEVERITIES:
        severity = "info"

    entities = raw.get("entities") or []
    if isinstance(entities, str):
        entities = [entities]
    entities = [str(e).strip() for e in entities if str(e).strip()][:10]

    return ExtractedItem(
        kind=kind,
        text=text[:200],
        source_span=str(raw.get("source_span", "")).strip()[:200],
        topic=str(raw.get("topic", "")).strip()[:60],
        entities=entities,
        context=str(raw.get("context", "")).strip()[:200],
        rationale=str(raw.get("rationale", "")).strip()[:240],
        confidence=max(0.0, min(1.0, confidence)),
        priority=max(1, min(5, priority)),
        severity=severity,
    )


def extract_items(
    title: str,
    body: str,
    project: str | None = None,
    capability: str = "standard",
) -> list[ExtractedItem]:
    """Run one note through the bridge. Returns [] on any failure.

    Best-effort by contract: this runs on a cron, and a bridge outage must
    never poison the watermark or raise into the daemon. The caller only
    advances a note's watermark when this returns successfully — see
    engine.extract_once.
    """
    if len(body.strip()) < MIN_BODY_LEN:
        return []

    from okuro.bridge.invoke import invoke

    result = invoke(
        prompt=build_prompt(title, body, project),
        capability=capability,
        system_prompt=_SYSTEM_PROMPT,
        # Stateless tool-function call: no MCP servers, no CLAUDE.md discovery,
        # no per-machine prompt sections (~3x faster on claude).
        tool=True,
        response_format="json",
        temperature=0.1,
    )

    if not result.get("success"):
        log.warning("notes_extract bridge failed: %s", result.get("error"))
        raise BridgeUnavailable(result.get("error") or "bridge invoke failed")

    parsed = _parse(result.get("output", ""))
    raw_items = parsed.get("items")
    if not isinstance(raw_items, list):
        log.debug("notes_extract: no items array in output")
        return []

    items: list[ExtractedItem] = []
    seen: set[str] = set()
    for raw in raw_items[:8]:
        item = _coerce_item(raw)
        if item is None:
            continue
        # Model-level duplicate guard — cheaper than a ledger round-trip.
        norm = _normalize(item.text)
        if norm in seen:
            continue
        seen.add(norm)
        items.append(item)

    return items


class BridgeUnavailable(RuntimeError):
    """The LLM call failed — distinct from 'the note had nothing in it'.

    Separating these matters for the watermark: a note that genuinely yields
    zero items is done and should be marked processed, while a note whose
    bridge call errored must stay unwatermarked so the next tick retries it.
    """


__all__ = [
    "ExtractedItem",
    "BridgeUnavailable",
    "extract_items",
    "build_prompt",
    "item_key",
    "verified_span",
    "MIN_BODY_LEN",
]
