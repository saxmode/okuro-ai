# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The multi-model adversary panel — okuro·prism's sparring core. Fan a
#   claim/ask out to several DIFFERENT bridge providers, each playing a hostile
#   high-player, collect their strongest objections, rank by damage, and return a
#   `redteam` block (+ a decisionrecord scaffold). The moat: not one model's
#   blind spots — a panel of distinct minds attacking.
# index:
#   def _adversary_prompt
#   def _parse_objections
#   def red_team
# AGENT_HEADER_END -->
"""Red-team a position with a panel of distinct models.

Prism generation is a monologue; this is the contest. Given the content under
scrutiny, each available provider (claude / gemini / codex …) is prompted as a
hostile, sophisticated adversary — the skeptical board member — and returns the
objections that would break the argument. Their findings are merged, ranked by
severity, and rendered as a `redteam` block whose rows carry WHICH mind raised
each objection (the multi-model provenance no single-model tool can show).

Pure-ish: the only side effect is the bridge calls. Degrades gracefully — a
dead provider is skipped, zero providers → empty block (caller no-ops).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

log = logging.getLogger("okuro.prism.redteam")

_SEVERITY = ("high", "med", "low")

_SYSTEM = (
    "You are a hostile, sophisticated adversary in a high-stakes sparring "
    "session — a skeptical board member, a rival CEO, a regulator. Your job is "
    "to BREAK the argument put in front of you, not to be fair. Find the "
    "objections that do real damage: the load-bearing assumption that fails, "
    "the number that won't survive scrutiny, the risk being waved away, the "
    "cheaper option not considered. For each, give the strongest STEELMAN "
    "rebuttal the proponent could offer — so they walk in ready.\n"
    "Return ONLY a JSON array, most damaging first:\n"
    '[{"objection": str, "severity": "high|med|low", "rebuttal": str}]\n'
    "3-5 objections. No preamble, no prose outside the JSON."
)


def _adversary_prompt(content: str, audience_hint: str = "") -> str:
    aud = f"\nThe proponent is pitching to: {audience_hint}\n" if audience_hint else ""
    return (
        f"Stress-test this position. Attack it where it is weakest.{aud}\n"
        f"POSITION:\n{content[:6000]}\n\n"
        "Return the JSON array of objections now."
    )


def _parse_objections(output: str) -> list[dict[str, Any]]:
    """Extract the JSON objection array from a provider's reply (tolerant)."""
    text = (output or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    raw = text[start:end + 1]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        try:
            import json_repair
            data = json_repair.loads(raw)
        except Exception:
            return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for it in data:
        if not isinstance(it, dict) or not it.get("objection"):
            continue
        sev = str(it.get("severity", "")).lower()
        out.append({
            "objection": str(it["objection"]).strip(),
            "severity": sev if sev in _SEVERITY else "med",
            "rebuttal": (str(it["rebuttal"]).strip() if it.get("rebuttal") else None),
        })
    return out


def _available_adversaries(max_panel: int) -> list[str]:
    try:
        from okuro.bridge.providers import list_providers
        avail = [p["id"] for p in list_providers() if p.get("available")]
    except Exception as exc:  # noqa: BLE001
        log.warning("adversary panel: provider listing failed: %s", exc)
        return []
    return avail[:max_panel]


def red_team(
    content: str,
    *,
    audience_hint: str = "",
    providers: Optional[list[str]] = None,
    max_panel: int = 3,
    max_challenges: int = 8,
    provider_timeout: int = 120,
    source_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Run the adversary panel over ``content`` → a `redteam` block dict.

    Each provider in the panel is invoked independently as a hostile adversary;
    objections are tagged with the provider that raised them, deduped, and
    ranked high→med→low. Returns a block with an empty ``challenges`` list when
    no provider is reachable (the caller then skips attaching it).
    """
    from okuro.bridge.invoke import invoke

    panel = providers or _available_adversaries(max_panel)
    challenges: list[dict[str, Any]] = []
    used: list[str] = []
    seen: set[str] = set()

    for pid in panel:
        try:
            res = invoke(
                prompt=_adversary_prompt(content, audience_hint),
                system_prompt=_SYSTEM, provider=pid, tool=True,
                timeout=provider_timeout,
            )
        except Exception as exc:  # noqa: BLE001 — a dead adversary must not sink the panel
            log.warning("adversary %s failed: %s", pid, exc)
            continue
        if not (isinstance(res, dict) and res.get("success")):
            log.info("adversary %s returned no usable output", pid)
            continue
        objs = _parse_objections(res.get("output") or "")
        if objs:
            used.append(pid)
        for o in objs:
            key = re.sub(r"\W+", " ", o["objection"].lower()).strip()[:80]
            if key in seen:
                continue
            seen.add(key)
            challenges.append({**o, "who": pid})

    order = {s: i for i, s in enumerate(_SEVERITY)}
    challenges.sort(key=lambda c: order.get(c["severity"], 1))
    total = len(challenges)
    if max_challenges and total > max_challenges:
        challenges = challenges[:max_challenges]  # keep the most damaging; board-grade

    block: dict[str, Any] = {
        "type": "redteam",
        "challenges": challenges,
        "panel": used,
    }
    if used:
        shown = len(challenges)
        more = f" (top {shown} of {total})" if total > shown else ""
        block["caption"] = (
            f"{total} objections from a {len(used)}-model adversary panel "
            f"({', '.join(used)}){more}."
        )
    if source_ref:
        block["source_ref"] = source_ref
    return block


__all__ = ["red_team"]
