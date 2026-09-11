# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.morning_brief — morning audio-brief SCRIPT COMPOSER.
#   Turns yesterday's mined okuro activity (assets/work, cross-project
#   combinations, today's action items) into a spoken-ready script string.
#   Deterministic template, not an LLM bridge call — the structural contract
#   (verbatim opener, section order, exactly 3 action items) must hold every
#   single morning, not "most mornings". TTS rendering and scheduling are
#   separate subtasks; this module only composes text.
# index: imports | class BriefCompositionError | class MorningBriefFindings |
#   def compose_script | def _join_clauses | def _prose_actions
# AGENT_HEADER_END -->
"""Morning-brief script composer.

Consumes a :class:`MorningBriefFindings` (the output of mining okuro's last
24h of memory/progress/artifacts/thoughts/sessions/todos/KG — see subtask
1.1's research) and renders the fixed four-part spoken script:

1. verbatim opener — ``"Hey {user_name},"``
2. what was created / worked on yesterday
3. the interesting combinations / potential the research surfaced
4. exactly three action items for today

Downstream (a later subtask) feeds the returned string into the existing
edition-tiered TTS channel (``okuro.peer.delivery.channels.summary``) — this
module has no audio/TTS/network dependency so it stays trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BriefCompositionError(ValueError):
    """Raised when findings don't satisfy the morning-brief contract."""


@dataclass
class MorningBriefFindings:
    """Structured input for :func:`compose_script`.

    ``action_items`` MUST contain exactly 3 entries — that's the whole point
    of a morning brief (board-grade, no filler): it forces the researcher to
    pick the 3 things that matter instead of dumping a todo list.
    """

    user_name: str
    assets: list[str] = field(default_factory=list)
    combinations: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)


def compose_script(findings: MorningBriefFindings) -> str:
    """Render ``findings`` into the fixed 4-section spoken script.

    Raises :class:`BriefCompositionError` if the structural contract isn't
    met (missing name, no assets/combinations, or action_items count != 3).
    """
    if not findings.user_name.strip():
        raise BriefCompositionError("morning brief requires a user_name")
    if not findings.assets:
        raise BriefCompositionError("morning brief requires at least one asset/work item")
    if not findings.combinations:
        raise BriefCompositionError("morning brief requires at least one combination item")
    if len(findings.action_items) != 3:
        raise BriefCompositionError(
            f"morning brief requires exactly 3 action items, got {len(findings.action_items)}"
        )

    parts = [
        f"Hey {findings.user_name.strip()},",
        "",
        "Yesterday: " + _join_clauses(findings.assets),
        "",
        "Here's what's interesting: " + _join_clauses(findings.combinations),
        "",
        _prose_actions(findings.action_items),
    ]
    return "\n".join(parts).strip() + "\n"


def _join_clauses(items: list[str]) -> str:
    """Join clauses into flowing spoken prose — one closed sentence each."""
    out = []
    for item in items:
        clause = item.strip()
        if not clause:
            continue
        if not clause.endswith((".", "!", "?")):
            clause += "."
        out.append(clause)
    return " ".join(out)


_ORDINALS = ("One", "Two", "Three")


def _prose_actions(action_items: list[str]) -> str:
    clauses = []
    for word, item in zip(_ORDINALS, action_items):
        clause = item.strip().rstrip(".")
        clauses.append(f"{word}, {clause}.")
    return "Three things for today: " + " ".join(clauses)
