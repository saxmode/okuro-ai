# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Text → IntentDecision. Keyword-based parser; first 5 tokens
#   (or leading-line / post-colon) determine intent. Multi-intent
#   detected → caller surfaces a disambiguation prompt. Param extraction
#   for orchestrator (intelligence, body), reminder (time), todo
#   (priority) happens here.
# index: imports | constants | IntentDecision | classify
# AGENT_HEADER_END -->
"""Keyword router for ingress text → action intent."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("okuro.ingress.router")

# Intent → list of trigger keywords. Lowercased, whole-word match.
INTENT_KEYWORDS: dict[str, list[str]] = {
    "orchestrator_task": ["orchestrator", "orchestrate", "dispatch"],
    "reminder":          ["remind", "reminder"],
    "todo":              ["todo", "to-do"],
    "thought":           ["thought", "idea", "note"],
    "question":          ["ask", "question"],
}

# Intent risk tier — high requires challenge-response. Aligned with the
# decision matrix the user approved.
HIGH_STAKES = {"orchestrator_task", "reminder"}

VALID_INTELLIGENCE = ("low", "balanced", "high", "max")

# Where in the message we treat keywords as "leading" — only matches in
# the first 5 tokens or as the first word after a leading colon/heading.
LEADING_TOKEN_BUDGET = 5


@dataclass
class IntentDecision:
    intent: str
    """Always set. One of INTENT_KEYWORDS keys or 'thought' fallback."""
    payload: dict = field(default_factory=dict)
    confidence: float = 1.0
    requires_challenge: bool = False
    ambiguous: bool = False
    """True when multiple distinct intents matched in the leading region."""
    candidates: list[str] = field(default_factory=list)
    """Populated only when ambiguous=True."""


# ── Helpers ──────────────────────────────────────────────────────────


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _leading_intents(text: str) -> list[str]:
    """Return the set of intents matched in the leading region of the text.

    Leading region = first LEADING_TOKEN_BUDGET tokens, OR any token
    preceded by a colon at the start of a line.
    """
    tokens = _tokens(text)
    leading = set(tokens[:LEADING_TOKEN_BUDGET])

    # After-colon trigger: "Re: orchestrator task" or "Orchestrator: …".
    for line in text.splitlines():
        if ":" in line:
            head, _, tail = line.partition(":")
            for t in _tokens(head):
                leading.add(t)
            for t in _tokens(tail)[:LEADING_TOKEN_BUDGET]:
                leading.add(t)

    matched: list[str] = []
    for intent, kws in INTENT_KEYWORDS.items():
        for kw in kws:
            if kw in leading:
                matched.append(intent)
                break
    return matched


_INTELLIGENCE_RE = re.compile(
    r"\b(?:with|using|at|in|on|---)?\s*(low|balanced|high|max|maximum)\s+intelligence\b",
    flags=re.IGNORECASE,
)
_INTELLIGENCE_RE_REVERSED = re.compile(
    r"\bintelligence\s*[:=]?\s*(low|balanced|high|max|maximum)\b",
    flags=re.IGNORECASE,
)


def _extract_intelligence(text: str) -> Optional[str]:
    for rx in (_INTELLIGENCE_RE, _INTELLIGENCE_RE_REVERSED):
        m = rx.search(text)
        if m:
            v = m.group(1).lower()
            if v == "maximum":
                v = "max"
            if v in VALID_INTELLIGENCE:
                return v
    return None


def _strip_intent_keywords(text: str) -> str:
    """Trim "dispatch an orchestrator task" / "orchestrator:" / etc.
    from the front so the body reads cleanly. Best-effort, not surgical."""
    body = text.strip()
    # Drop a leading "<keyword>:" header line entirely.
    head, sep, tail = body.partition("\n")
    if sep and head.lower().rstrip(":").strip() in (
        kw for kws in INTENT_KEYWORDS.values() for kw in kws
    ):
        body = tail.strip()

    # Strip a "dispatch[ an] orchestrator task[ to]" prefix.
    body = re.sub(
        r"^\s*(please\s+)?(dispatch|launch|start|create|run)\b\s+"
        r"(an?\s+)?(orchestrator)\s*(task)?\s*(to\s+)?[:,]?\s*",
        "",
        body,
        flags=re.IGNORECASE,
    )
    # Strip a bare "orchestrator[:]" prefix.
    body = re.sub(
        r"^\s*orchestrator[:,\.\s]+",
        "",
        body,
        flags=re.IGNORECASE,
    )
    # Strip a "todo[:]" / "thought[:]" / "remind me to" / "remind me" prefix.
    body = re.sub(
        r"^\s*(todo|to-do|thought|idea|note|ask|question)[:,\.\s]+",
        "",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(
        r"^\s*remind(?:er)?(?:\s+me)?(?:\s+to)?[:,\.\s]+",
        "",
        body,
        flags=re.IGNORECASE,
    )
    # Drop a trailing "with X intelligence" phrase from the body.
    body = _INTELLIGENCE_RE.sub("", body).strip(" .,;-")
    return body.strip() or text.strip()


# ── Public API ───────────────────────────────────────────────────────


def classify(text: str) -> IntentDecision:
    """Decide which intent a message expresses + extract payload."""
    matched = _leading_intents(text)

    if not matched:
        # Fallback — treat as a thought so nothing is silently dropped.
        return IntentDecision(
            intent="thought",
            payload={"body": text.strip()},
            confidence=0.4,
            requires_challenge=False,
        )

    if len(matched) > 1:
        return IntentDecision(
            intent=matched[0],
            payload={"body": text.strip()},
            confidence=0.5,
            requires_challenge=False,
            ambiguous=True,
            candidates=sorted(set(matched)),
        )

    intent = matched[0]
    body = _strip_intent_keywords(text)
    payload: dict = {"body": body}

    if intent == "orchestrator_task":
        intelligence = _extract_intelligence(text) or "balanced"
        # The ingress challenge IS the approval gate — don't double-gate
        # via the orchestrator's deliberate mode. auto-execute runs the
        # plan straight through.
        payload = {
            "description": body,
            "intelligence": intelligence,
            "mode": "auto-execute",
        }

    elif intent == "reminder":
        # Time extraction is deferred to the dispatcher (dateparser).
        payload = {"text": body}

    elif intent == "todo":
        payload = {"title": body, "priority": "P2"}

    elif intent == "thought":
        payload = {"body": body}

    elif intent == "question":
        payload = {"task_hint": body}

    return IntentDecision(
        intent=intent,
        payload=payload,
        confidence=0.9,
        requires_challenge=intent in HIGH_STAKES,
    )
