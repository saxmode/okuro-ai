# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Behavioral rules — structured triples (rule/rationale/evidence) for Meta-Harness P10.
# index: imports | EM_DASH | SECTION_PATHS | parse_triple | split_inline_rationale | load_section | load_all | render_line | find_rule | auto_enrich_async | enrich
# AGENT_HEADER_END -->
"""Behavioral rules — structured triples.

Backs Meta-Harness P10 (skill = safety rails, not diagnosis procedure):
every directive agents receive in bootstrap should expose its *rationale*
so a capable model can judge edge cases, not just blindly apply the rule.

This module is a thin reader layer over the existing ``user_profile``
JSON. Rules are stored in-place in the profile under their natural
section paths (``communication.patterns``, ``cognitive_style.implications``,
etc.) and may be in two shapes:

1. **Legacy string** — possibly with em-dash separating rule and
   rationale: ``"lead with the answer, then explain if needed"`` or
   ``"monotropic attention — finish current topic before switching"``.
2. **Structured triple** — ``{"rule": "...", "rationale": "...",
   "evidence": "..."}``.

``parse_triple`` normalizes both shapes into the same dict. Rendering
back to a single line is backwards-compatible: triples with a rationale
re-join with an em-dash, so the bootstrap packet looks identical unless
an evidence field is present.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

EM_DASH = "—"
HYPHEN_SEP_ALT = " - "  # some legacy entries use hyphen + spaces instead of em-dash

# Section paths that hold behavioral rules. Ordered for stable rendering.
SECTION_PATHS: list[tuple[str, str, str]] = [
    # (section_id, dotted_path_into_profile, human-label)
    ("implications",  "cognitive_style.implications",   "Cognitive implications"),
    ("patterns",      "communication.patterns",         "Communication rules"),
    ("pet_peeves",    "communication.pet_peeves",       "Pet peeves"),
    ("avoid",         "communication.format_preferences.avoid",    "Avoid"),
    ("preferred",     "communication.format_preferences.preferred", "Preferred formats"),
    ("hates",         "work_style.hates",               "User hates"),
    ("loves",         "work_style.loves",               "User loves"),
    ("strengths",     "cognitive_style.strengths",      "Strengths"),
]


def split_inline_rationale(text: str) -> tuple[str, str | None]:
    """Split a legacy string into (rule, rationale).

    Recognizes em-dash as the canonical separator. Returns rationale=None
    when no separator is present.
    """
    s = (text or "").strip()
    if not s:
        return s, None
    if EM_DASH in s:
        rule, _, rationale = s.partition(EM_DASH)
        rule = rule.strip().rstrip(",")
        rationale = rationale.strip()
        return rule, rationale or None
    # Some entries use " - " instead of em-dash; keep conservative so we
    # don't accidentally split things like "write code - test - commit".
    if HYPHEN_SEP_ALT in s and s.count(HYPHEN_SEP_ALT) == 1:
        rule, _, rationale = s.partition(HYPHEN_SEP_ALT)
        return rule.strip(), rationale.strip() or None
    return s, None


def parse_triple(item: Any) -> dict[str, Any]:
    """Normalize a rule entry to a triple dict.

    Accepts:
      * a string (possibly with inline rationale after an em-dash)
      * a pre-structured ``{"rule", "rationale", "evidence"}`` dict

    Unknown shapes are coerced to ``str(item)`` with no rationale.
    """
    if isinstance(item, dict):
        rule = str(item.get("rule", "")).strip()
        rationale = item.get("rationale")
        evidence = item.get("evidence")
        if rationale is not None:
            rationale = str(rationale).strip() or None
        if evidence is not None:
            evidence = str(evidence).strip() or None
        if rule:
            return {"rule": rule, "rationale": rationale, "evidence": evidence}
        # dict missing 'rule' — fall through to string coercion for safety
        return {"rule": str(item), "rationale": None, "evidence": None}

    if isinstance(item, str):
        rule, rationale = split_inline_rationale(item)
        return {"rule": rule, "rationale": rationale, "evidence": None}

    return {"rule": str(item), "rationale": None, "evidence": None}


def _walk(profile: dict, dotted_path: str) -> Any:
    """Dot-path accessor into a nested dict. Returns None on any miss."""
    cur: Any = profile
    for part in dotted_path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def load_section(section_id: str, profile: dict | None = None) -> list[dict]:
    """Load one behavioral-rule section as a list of triples."""
    if profile is None:
        from okuro.yu.profile import get_profile_raw
        profile = get_profile_raw() or {}
    for sid, path, _label in SECTION_PATHS:
        if sid != section_id:
            continue
        raw = _walk(profile, path)
        if isinstance(raw, list):
            return [parse_triple(item) for item in raw]
        return []
    raise KeyError(f"unknown behavioral-rule section: {section_id}")


def load_all(profile: dict | None = None) -> dict[str, dict[str, Any]]:
    """Load every known section. Returns ``{section_id: {label, path, rules}}``."""
    if profile is None:
        from okuro.yu.profile import get_profile_raw
        profile = get_profile_raw() or {}
    out: dict[str, dict[str, Any]] = {}
    for sid, path, label in SECTION_PATHS:
        raw = _walk(profile, path)
        rules = [parse_triple(item) for item in raw] if isinstance(raw, list) else []
        out[sid] = {"label": label, "path": path, "rules": rules}
    return out


def render_line(triple: dict, *, include_evidence: bool = False) -> str:
    """Render one triple to a single bullet-line string.

    Backwards-compatible with the legacy flat-string format: when a
    rationale is present we re-join with an em-dash, so existing
    bootstrap packets keep rendering identically for the current
    profile. ``include_evidence=True`` is used by CLI / MCP outputs
    that want the full triple on a line.
    """
    rule = triple.get("rule", "")
    rationale = triple.get("rationale")
    evidence = triple.get("evidence")
    if rationale:
        line = f"{rule} {EM_DASH} {rationale}"
    else:
        line = rule
    if include_evidence and evidence:
        line += f"  [evidence: {evidence}]"
    return line


def find_rule(query: str, *, limit: int = 5, profile: dict | None = None) -> list[dict]:
    """Fuzzy-match a rule text across every section.

    Returns a list of ``{section_id, label, rule, rationale, evidence, score}``
    sorted by match strength, up to ``limit`` results.
    """
    if not query:
        return []
    haystack: list[dict] = []
    for sid, bundle in load_all(profile).items():
        for rule in bundle["rules"]:
            haystack.append({
                "section_id": sid,
                "label": bundle["label"],
                **rule,
            })
    if not haystack:
        return []
    rule_texts = [h["rule"] for h in haystack]
    matches = difflib.get_close_matches(query, rule_texts, n=limit, cutoff=0.3)
    out: list[dict] = []
    for m in matches:
        for h in haystack:
            if h["rule"] == m and h not in out:
                score = difflib.SequenceMatcher(None, m.lower(), query.lower()).ratio()
                out.append({**h, "score": round(score, 3)})
                break
    return out


def iter_rules(profile: dict | None = None) -> Iterable[dict]:
    """Yield every rule across every section (section_id included)."""
    for sid, bundle in load_all(profile).items():
        for rule in bundle["rules"]:
            yield {"section_id": sid, **rule}


_ENRICH_PROMPT = """You are annotating a user's behavioral contract with rationales
agents can use to judge edge cases. Preserve the user's voice and
terseness. Each rationale should explain WHY this directive exists in one short
clause, drawn from the user's cognitive/communication profile below.

## User profile (for context)
Neurotype: {neurotype}
Known traits/implications (with rationales already):
{examples}

## Rules needing rationale
{rules_block}

## Task
For each rule listed above, produce:
- `rationale`: one clause (≤18 words) explaining the cognitive or workflow reason
- `evidence`: pointer to the profile section that supports it (one of: cognitive_style.implications, cognitive_style.strengths, communication.pet_peeves, communication.format_preferences, work_style.hates, work_style.loves, decision_style, neurotype). Use the most specific single path.

## Output
STRICT JSON, no prose, no markdown fence:
{{"<section_id>": {{"<exact rule text>": {{"rationale": "...", "evidence": "..."}}, ...}}, ...}}
Include only sections/rules that were in the input.
"""


def _collect_enrichment_targets(profile: dict) -> dict[str, list[str]]:
    """Per-section list of rule texts that still lack a rationale."""
    targets: dict[str, list[str]] = {}
    for sid, bundle in load_all(profile).items():
        missing = [r["rule"] for r in bundle["rules"] if not r["rationale"]]
        if missing:
            targets[sid] = missing
    return targets


def _build_enrich_prompt(profile: dict, targets: dict[str, list[str]]) -> str:
    cog = profile.get("cognitive_style", {}) or {}
    neurotype = ", ".join(cog.get("neurotype", []) or []) or "unspecified"

    # Show a handful of existing rationale examples so the LLM matches voice.
    examples: list[str] = []
    for sid in ("implications", "patterns", "pet_peeves"):
        try:
            for r in load_section(sid, profile):
                if r["rationale"] and len(examples) < 8:
                    examples.append(f"  - {r['rule']} — {r['rationale']}")
        except KeyError:
            pass
    examples_block = "\n".join(examples) or "  (none yet)"

    rules_block_lines: list[str] = []
    for sid, rule_list in targets.items():
        rules_block_lines.append(f"### {sid}")
        for r in rule_list:
            rules_block_lines.append(f"  - {r}")
    rules_block = "\n".join(rules_block_lines)

    return _ENRICH_PROMPT.format(
        neurotype=neurotype,
        examples=examples_block,
        rules_block=rules_block,
    )


def _merge_enrichment(profile: dict, enrichment: dict) -> tuple[dict, int]:
    """Apply enrichment into profile in-place. Returns (profile, count_updated).

    Converts string rules to triple dicts only when a rationale was produced.
    Preserves legacy structure otherwise.
    """
    updated = 0
    section_paths = {sid: path for sid, path, _ in SECTION_PATHS}
    for sid, per_rule in (enrichment or {}).items():
        if sid not in section_paths or not isinstance(per_rule, dict):
            continue
        path = section_paths[sid]

        # Walk + split section path to locate the list
        parts = path.split(".")
        parent = profile
        for p in parts[:-1]:
            if not isinstance(parent, dict) or p not in parent:
                parent = None
                break
            parent = parent[p]
        if parent is None:
            continue
        key = parts[-1]
        current = parent.get(key)
        if not isinstance(current, list):
            continue

        new_list = []
        for item in current:
            triple = parse_triple(item)
            # Only replace when LLM returned a rationale for the exact rule text
            payload = per_rule.get(triple["rule"])
            if payload and isinstance(payload, dict) and payload.get("rationale") and not triple["rationale"]:
                triple["rationale"] = str(payload["rationale"]).strip()
                if payload.get("evidence"):
                    triple["evidence"] = str(payload["evidence"]).strip()
                new_list.append({
                    "rule": triple["rule"],
                    "rationale": triple["rationale"],
                    "evidence": triple["evidence"],
                })
                updated += 1
            else:
                new_list.append(item)
        parent[key] = new_list
    return profile, updated


_AUTO_ENRICH_LOCK = None  # lazy-init threading.Lock (avoid import at module load)


def auto_enrich_async(*, provider: str | None = None) -> bool:
    """Fire-and-forget enrich if the profile has rules without rationale.

    Safe to call from any write path — returns immediately. Spawns a
    daemon thread that runs :func:`enrich`. Guarded against concurrent
    runs by a module-level lock, so rapid successive saves don't stack
    multiple LLM calls.

    ``provider=None`` (the default) lets :func:`okuro.bridge.invoke`
    route via the user's configured ``inference.routing.default``, so
    the hook is not Claude-specific — gemini / codex / local work
    equally well when the user picks them in ``~/.okuro/config.yaml``.

    Returns True if a run was scheduled, False if there was nothing to
    enrich or another run is already in flight.
    """
    global _AUTO_ENRICH_LOCK
    if _AUTO_ENRICH_LOCK is None:
        import threading
        _AUTO_ENRICH_LOCK = threading.Lock()

    if not _AUTO_ENRICH_LOCK.acquire(blocking=False):
        logger.debug("auto_enrich skipped — another run in flight")
        return False

    try:
        # Cheap pre-check — avoid spawning a thread if there's nothing to do.
        from okuro.yu.profile import get_profile_raw
        profile = get_profile_raw() or {}
        targets = _collect_enrichment_targets(profile)
        if not targets:
            _AUTO_ENRICH_LOCK.release()
            return False
    except Exception:
        _AUTO_ENRICH_LOCK.release()
        return False

    def _run() -> None:
        try:
            result = enrich(provider=provider, dry_run=False)
            logger.info(
                "auto_enrich: targets=%s updated=%s persisted=%s model=%s",
                result.get("targets"),
                result.get("updated"),
                result.get("persisted"),
                result.get("model"),
            )
        except Exception as exc:
            logger.warning("auto_enrich failed: %s", exc)
        finally:
            try:
                _AUTO_ENRICH_LOCK.release()
            except RuntimeError:
                pass

    import threading
    threading.Thread(target=_run, name="okuro-auto-enrich", daemon=True).start()
    return True


def enrich(*, provider: str | None = None, dry_run: bool = False) -> dict:
    """LLM-generate rationale+evidence for every rule that lacks one.

    One batch call to the bridge — cheaper and more consistent in voice
    than per-rule calls. ``provider=None`` routes via the bridge's
    configured default, so this function is not Claude-specific; any
    provider in ``~/.okuro/config.yaml`` works. Returns
    ``{targets, updated, persisted, model}``.
    """
    from okuro.yu.profile import get_profile_raw, update_profile
    from okuro.bridge.invoke import invoke
    import json as _json

    profile = get_profile_raw() or {}
    targets = _collect_enrichment_targets(profile)
    if not targets:
        return {"targets": 0, "updated": 0, "persisted": False, "model": None}

    prompt = _build_enrich_prompt(profile, targets)
    result = invoke(
        prompt=prompt,
        provider=provider,
        system_prompt=(
            "You output only valid JSON. No commentary, no markdown fences. "
            "Match the user's terse, literal voice in rationales."
        ),
    )
    if not result.get("success"):
        return {
            "targets": sum(len(v) for v in targets.values()),
            "updated": 0,
            "persisted": False,
            "model": result.get("model"),
            "error": result.get("error") or "invoke failed",
        }

    output = result.get("output", "")
    start, end = output.find("{"), output.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {
            "targets": sum(len(v) for v in targets.values()),
            "updated": 0,
            "persisted": False,
            "model": result.get("model"),
            "error": "no JSON object in LLM output",
        }
    try:
        enrichment = _json.loads(output[start : end + 1])
    except _json.JSONDecodeError as exc:
        return {
            "targets": sum(len(v) for v in targets.values()),
            "updated": 0,
            "persisted": False,
            "model": result.get("model"),
            "error": f"json parse: {exc}",
        }

    new_profile, updated = _merge_enrichment(profile, enrichment)
    persisted = False
    if updated and not dry_run:
        for sid, path, _label in SECTION_PATHS:
            if sid not in enrichment:
                continue
            parts = path.split(".")
            top = parts[0]
            sub_path = ".".join(parts[1:])
            # Walk into the new profile to get the updated list
            cur = new_profile.get(top)
            for p in parts[1:]:
                cur = cur.get(p) if isinstance(cur, dict) else None
                if cur is None:
                    break
            if isinstance(cur, list):
                update_profile(section=top, path=sub_path, action="set", value=cur)
        persisted = True

    return {
        "targets": sum(len(v) for v in targets.values()),
        "updated": updated,
        "persisted": persisted,
        "model": result.get("model"),
        "enrichment": enrichment if dry_run else None,
    }
