# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism Scenario Weaver — choose ONE concrete running case and
#   thread it across every topic (a beat per topic) so the abstract lands and the
#   deck is memorable. Stage 5 of the root-doc→deck pipeline; runs ACROSS the
#   whole tree (not a per-topic fan-out). Implements prism-scenario-weaver charter.
# index: _WEAVE_SYSTEM | weave_scenario
# AGENT_HEADER_END -->
"""Prism Scenario Weaver.

Picks one concrete running case (the "a visitor arrives" / butler case in the
reference board deck) and threads a beat of it through every topic. This is the
single biggest reason a board deck reads memorable rather than merely correct.
Runs once over the WHOLE tree — it is the one non-parallel stage, since a beat
must advance the same story across topics.

Mirrors the ``prism-scenario-weaver`` charter (roles/catalog).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.weaver")

_WEAVE_SYSTEM = (
    "You are okuro·prism's SCENARIO WEAVER. Given a deck's TOPIC TREE and its CLAIMS, "
    "choose ONE concrete running case and thread it across the topics — one beat per topic "
    "— so an abstract analysis becomes a story the reader remembers. Output ONLY a JSON "
    "object.\n\n"
    "Rules:\n"
    "  1. Pick the ONE case that naturally touches the MOST topics (e.g. a concrete user/"
    "visitor/operator moment, a day-in-the-life, a single transaction). Define it concretely "
    "— who, where, what happens.\n"
    "  2. It must be PLAUSIBLE from the claims — never a capability or outcome the claims "
    "don't support. Ground it; don't wish-cast features.\n"
    "  3. Write ONE beat per topic: a 1-2 sentence moment that ADVANCES the same story while "
    "carrying THAT topic's point. Same characters, same world, one throughline start to end.\n"
    "  4. Keep it faithful and consistent — a later stage weaves each beat into its topic.\n\n"
    'Return exactly: {"case":str (2-3 sentences setting up the running case),'
    '"beats":[{"topic_id":str,"beat":str} ...]}.'
)


def weave_scenario(
    topics: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    *,
    brief: Optional[dict[str, Any]] = None,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Choose a running case and a per-topic beat over the whole tree.

    ``topics`` = top-level topics [{id, title, claim_ids}]; ``claims`` = the full
    claim list. Returns {"case": str, "beats": {topic_id: beat}} — beats keyed by
    topic id, filtered to real topic ids."""
    topics = [t for t in (topics or []) if t.get("id") and (t.get("title") or "").strip()]
    if not topics:
        return {"case": "", "beats": {}}

    by_id = {c.get("id"): (c.get("statement") or "").strip() for c in (claims or [])}
    lines: list[str] = []
    for t in topics:
        cids = (t.get("claim_ids") or [])[:6]
        gist = " | ".join(by_id.get(cid, "") for cid in cids if by_id.get(cid))
        lines.append(f"[{t['id']}] {t['title']} — {gist[:300]}")
    tree_block = "\n".join(lines)
    angle = (brief or {}).get("angle") or ""
    user = (
        (f"AUDIENCE ANGLE: {angle}\n\n" if angle else "")
        + f"TOPIC TREE (id | title | key claims):\n{tree_block}\n\n"
        "Choose the running case and one beat per topic. Return ONLY the JSON object."
    )

    from okuro.bridge.invoke import invoke
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    _prev = os.environ.get("MAX_THINKING_TOKENS")
    os.environ["MAX_THINKING_TOKENS"] = "0"
    try:
        res = invoke(prompt=user, system_prompt=_WEAVE_SYSTEM, provider=provider, capability="standard", timeout=240)
    finally:
        if _prev is None:
            os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            os.environ["MAX_THINKING_TOKENS"] = _prev

    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")

    valid = {t["id"] for t in topics}
    case = ""
    beats: dict[str, str] = {}
    try:
        data = _lenient_json_loads(_extract_json_span(res.get("output") or "", "{", "}"))
        if isinstance(data, dict):
            case = (data.get("case") or "").strip()
            for b in (data.get("beats") or []):
                if isinstance(b, dict) and b.get("topic_id") in valid and (b.get("beat") or "").strip():
                    beats[b["topic_id"]] = b["beat"].strip()
    except (ValueError, KeyError) as exc:
        logger.warning("weave_scenario: parse failed: %s", exc)

    return {"case": case, "beats": beats}
