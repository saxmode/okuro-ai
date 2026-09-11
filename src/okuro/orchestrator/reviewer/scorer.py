# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: M3 Scorer stage — weighs Critic findings, emits verdict
#   (PASS|CONDITIONAL|FAIL) + per-dimension rubric. In-process via
#   bridge_invoke. Scorer CANNOT soften a load-bearing Critic FAIL —
#   that override is enforced in pipeline.run_review, but the prompt
#   here is also explicit so the LLM doesn't waste budget arguing.
# index: imports | _build_prompt | _parse_scorer | run_scorer
# AGENT_HEADER_END -->
"""Scorer — weighed verdict + rubric.

The Scorer reads the Critic's findings, looks at the deliverables, and
issues:

    verdict — PASS | CONDITIONAL | FAIL
    rubric  — {factual, consistency, completeness, scope_fit,
               acceptance_criteria_met} each 0–5

Hard rule (mirrored in :mod:`pipeline.run_review` enforcement): any
load-bearing Critic finding forces verdict ≤ FAIL. The Scorer can only
soften load-bearing findings if the Critic withdraws them — Scorer
cannot withdraw on Critic's behalf.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)


_PROMPT_TEMPLATE = """You are the SCORER. The CRITIC has produced a high-recall findings list. Your job is to WEIGH each finding (load-bearing vs cosmetic), score the deliverables across five dimensions, and emit a verdict.

# CONTRACT — what you can and cannot do

You CAN:
- Confirm a load-bearing finding (verdict goes to FAIL)
- Downgrade a finding the Critic labelled load-bearing to cosmetic when the evidence does not support load-bearing (you must justify in `notes`)
- Aggregate a CONDITIONAL verdict when only cosmetic findings remain
- Issue PASS when the findings list is empty AND every acceptance criterion has visible satisfaction in the deliverables

You CANNOT:
- Remove a Critic finding entirely — only the Critic can withdraw
- Issue PASS while ANY load-bearing finding remains (the pipeline will override you to FAIL — do not waste budget arguing)
- Invent new findings (your job is weighing, not flaw-finding — that was the Critic's pass)

# TASK
- id: {task_id}
- description: {task_description}
- phase: {phase_id} — {phase_name}

# ACTIVE ADRs
{adrs_block}

# CRITIC FINDINGS
{findings_block}

# DETERMINISTIC RESULTS (already certain — informational)
{det_block}

# DELIVERABLES UNDER REVIEW
{deliverables_block}

# OUTPUT FORMAT — emit ONLY a JSON object, nothing else

{{
  "verdict": "PASS" | "CONDITIONAL" | "FAIL",
  "rubric": {{
    "factual":                <int 0-5>,
    "consistency":            <int 0-5>,
    "completeness":           <int 0-5>,
    "scope_fit":              <int 0-5>,
    "acceptance_criteria_met":<int 0-5>
  }},
  "load_bearing_count": <int>,
  "cosmetic_count":     <int>,
  "weighed_findings": [
    {{
      "index":      <int — position in CRITIC FINDINGS list, 0-based>,
      "severity":   "load_bearing" | "cosmetic",
      "weight":     "block" | "soften" | "confirm",
      "notes":      "<one-sentence rationale for the weighing>"
    }}
  ],
  "rationale": "<2-4 sentences explaining the verdict choice; cite specific findings by index>"
}}

Rubric scale: 0 = absent, 1 = trace, 2 = partial, 3 = adequate, 4 = strong, 5 = exemplary.

Verdict rules:
- ≥1 load_bearing finding (after your weighing) → FAIL
- 0 load_bearing AND ≥1 cosmetic finding → CONDITIONAL
- 0 findings AND every acceptance criterion satisfied → PASS
- Default on uncertainty: FAIL (shipping-bias is the documented failure mode this Scorer guards against)
"""


def _format_findings(findings: list[dict]) -> str:
    if not findings:
        return "_(no findings — Critic found nothing)_"
    rows: list[str] = []
    for i, f in enumerate(findings):
        f = f or {}
        rows.append(
            f"{i}. [{f.get('severity', '?')}] {f.get('file', '?')}:{f.get('line', '?')} — "
            f"{f.get('summary', '')[:200]}\n"
            f"   evidence: {f.get('evidence', '')[:200]}\n"
            f"   suggested_fix: {f.get('suggested_fix', '')[:200]}"
        )
    return "\n".join(rows)


def _format_adrs_brief(adrs: list[dict]) -> str:
    if not adrs:
        return "_(none)_"
    return "\n".join(
        f"- {(a.get('prompt') or '?')[:120]} → `{(a.get('selected_label') or '?')[:80]}`"
        for a in adrs
    )


def _format_det_brief(det_results: list[Any]) -> str:
    rows: list[str] = []
    for r in det_results:
        kind = getattr(getattr(r, "check", None), "kind", None) or (r.get("kind") if isinstance(r, dict) else "?")
        sev = getattr(getattr(r, "check", None), "severity", None) or (r.get("severity") if isinstance(r, dict) else "?")
        passed = getattr(r, "passed", None) if not isinstance(r, dict) else r.get("passed")
        findings = getattr(r, "findings", None) if not isinstance(r, dict) else r.get("findings")
        if passed and not findings:
            continue
        n = len(findings or [])
        rows.append(f"- {kind} ({sev}): {n} finding(s)")
    return "\n".join(rows) or "_(deterministic stage clean)_"


def _format_deliverables_brief(deliverable_text: dict[str, str]) -> str:
    if not deliverable_text:
        return "_(none)_"
    parts: list[str] = []
    for path, body in deliverable_text.items():
        parts.append(f"### `{path}` ({len(body)} chars)\n```\n{body[:3000]}\n```")
    return "\n\n".join(parts)


def _build_prompt(
    *,
    task: Any,
    phase: Any,
    adrs: list[dict],
    critic_findings: list[dict],
    deterministic_results: list[Any],
    deliverable_text: dict[str, str],
) -> str:
    return _PROMPT_TEMPLATE.format(
        task_id=getattr(task, "id", "?"),
        task_description=(getattr(task, "description", "") or "")[:400],
        phase_id=getattr(phase, "id", "?"),
        phase_name=getattr(phase, "name", "?"),
        adrs_block=_format_adrs_brief(adrs),
        findings_block=_format_findings(critic_findings),
        det_block=_format_det_brief(deterministic_results),
        deliverables_block=_format_deliverables_brief(deliverable_text),
    )


_JSON_BLOCK_RE = re.compile(r"\{(?:[^{}]|(?:\{(?:[^{}]|(?:\{[^{}]*\}))*\}))*\}", re.DOTALL)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_scorer(raw: str) -> dict:
    if not raw:
        return {"verdict": "FAIL", "rubric": {}, "error": "empty output"}
    # Robust extraction (mirrors critic._parse_findings): tolerate a chatty
    # preamble before a ```json fence and braces nested inside string values.
    # The scorer payload (nested rubric + weighed_findings) defeats the
    # one-level _JSON_BLOCK_RE, so without this a preamble → default FAIL →
    # wasted retry round — the exact churn this guards against.
    from okuro.orchestrator.reviewer.critic import _extract_balanced_object

    candidates: list[str] = [m.group(1) for m in _FENCED_JSON_RE.finditer(raw)]
    candidates.append(
        re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    )
    candidates.append(raw)
    balanced = _extract_balanced_object(raw)
    if balanced:
        candidates.append(balanced)
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("verdict"):
            return _normalize_scorer(obj)
    for m in _JSON_BLOCK_RE.finditer(raw):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and obj.get("verdict"):
                return _normalize_scorer(obj)
        except json.JSONDecodeError:
            continue
    log.warning("scorer: failed to parse JSON; defaulting to FAIL")
    return {"verdict": "FAIL", "rubric": {}, "error": "parse failed", "raw": raw[:400]}


def _normalize_scorer(obj: dict) -> dict:
    verdict = str(obj.get("verdict") or "FAIL").upper()
    if verdict not in {"PASS", "CONDITIONAL", "FAIL"}:
        verdict = "FAIL"
    rubric_in = obj.get("rubric") or {}
    rubric: dict[str, int] = {}
    for k in ("factual", "consistency", "completeness", "scope_fit", "acceptance_criteria_met"):
        v = rubric_in.get(k)
        try:
            rubric[k] = max(0, min(5, int(v)))
        except (TypeError, ValueError):
            rubric[k] = 0
    return {
        "verdict": verdict,
        "rubric": rubric,
        "load_bearing_count": int(obj.get("load_bearing_count") or 0),
        "cosmetic_count": int(obj.get("cosmetic_count") or 0),
        "weighed_findings": [f for f in (obj.get("weighed_findings") or []) if isinstance(f, dict)][:50],
        "rationale": (obj.get("rationale") or "")[:1200],
    }


def run_scorer(
    *,
    task: Any,
    phase: Any,
    adrs: list[dict],
    critic_findings: list[dict],
    deterministic_results: list[Any],
    deliverable_text: dict[str, str],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 180,
    tasks_dir: Optional[Any] = None,
) -> dict:
    """Invoke the Scorer via bridge. Returns the parsed dict (verdict + rubric)."""
    # Fast path: no findings AND no deterministic load-bearing failures → PASS
    # without paying the LLM bill. The Scorer still runs in the contested case
    # where the Critic flagged something or the deterministic stage failed.
    det_load_bearing = any(
        (not getattr(r, "passed", True)) and getattr(getattr(r, "check", None), "severity", "") == "load_bearing"
        for r in (deterministic_results or [])
    )
    if not critic_findings and not det_load_bearing:
        return {
            "verdict": "PASS",
            "rubric": {
                "factual": 5, "consistency": 5, "completeness": 5,
                "scope_fit": 5, "acceptance_criteria_met": 5,
            },
            "load_bearing_count": 0,
            "cosmetic_count": 0,
            "weighed_findings": [],
            "rationale": "Critic found no issues and deterministic stage is clean — PASS without LLM weighing.",
            "fast_path": True,
        }

    prompt = _build_prompt(
        task=task,
        phase=phase,
        adrs=adrs,
        critic_findings=critic_findings,
        deterministic_results=deterministic_results,
        deliverable_text=deliverable_text,
    )
    from okuro.bridge.invoke import invoke as bridge_invoke
    from pathlib import Path as _Path

    # Stream Scorer thinking into the task's .activity.jsonl alongside
    # critic events — same mechanism as run_critic, different role tag.
    task_id_for_sink = getattr(task, "id", "") or ""
    phase_id_for_sink = getattr(phase, "id", "") or ""
    activity_sink: _Path | None = None
    sink_subtask = ""
    if task_id_for_sink:
        try:
            # WP7 — prefer the engine-threaded tasks_dir (same as run_critic);
            # fall back to load_config() only when unthreaded.
            if tasks_dir is not None:
                _base = _Path(tasks_dir)
            else:
                from okuro.orchestrator.config import load_config
                _base = load_config().tasks_dir
            activity_sink = _base / task_id_for_sink / ".activity.jsonl"
            sink_subtask = f"reviewer:phase{phase_id_for_sink}:scorer"
        except Exception:
            activity_sink = None

    _prov = provider or "claude"
    res = bridge_invoke(
        prompt=prompt,
        capability=None,
        provider=_prov,
        # Reviewer default — see critic.run_critic. Claude → cheap "haiku";
        # non-claude (codex/openai) → "" so its CLI picks the account default.
        model=model or ("haiku" if _prov == "claude" else ""),
        timeout=timeout,
        activity_sink=activity_sink,
        subtask_id=sink_subtask or None,
        role="scorer",
    )
    if not res.get("success"):
        return {
            "verdict": "FAIL",
            "rubric": {},
            "error": res.get("error") or "bridge_invoke failed",
            "rationale": "Scorer bridge call failed — defaulting to FAIL.",
        }
    parsed = _parse_scorer((res.get("output") or "").strip())
    return parsed
