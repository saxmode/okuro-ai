# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: M3 Critic stage — high-recall, FAIL-default flaw finder.
#   In-process via bridge_invoke (mirrors M2 compressor). Reads
#   deterministic results + deliverables + active ADRs + acceptance
#   criteria; emits structured findings. The Critic OWNS recall —
#   the Scorer cannot soften a load-bearing Critic finding to PASS.
# index: imports | _has_declared_acs | _build_prompt | _parse_findings | run_critic
# AGENT_HEADER_END -->
"""Critic — high-recall flaw finder.

The Critic exists because monolithic LLM reviewers are shipping-biased
(arxiv 2508.07805 found +8% inflated scores when persuaded). Splitting
recall (here) from weighing (Scorer) is the Reflexion pattern (arxiv
2303.11366) and the Agent-as-Judge survey's recommended structure
(arxiv 2508.02994).

Output contract: a JSON object ``{"findings": [...]}`` where each finding
has ``severity``, ``file``, ``line``, ``evidence``, ``summary``,
``suggested_fix``. Parser is tolerant — extracts the first JSON block
from the bridge output, falls back to single-finding-from-text if the
LLM ignored the schema (and downgrades the verdict to FAIL accordingly).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_PROMPT_TEMPLATE = """You are the CRITIC. Your job is RECALL — flag every potential problem in the deliverables below. False positives are acceptable; missed issues are not. The Scorer (a separate role) will weigh your findings. You do not decide PASS/FAIL — you exhaustively enumerate problems.

# DEFAULT VERDICT IS FAIL
Unless your findings list is empty, the verdict is FAIL. Do not soften this in your output. Your job is to find things; if you find them, you flag them.

# TASK
- id: {task_id}
- description: {task_description}
- phase: {phase_id} — {phase_name}

# ACTIVE ADRs (user-locked, cannot be overridden)
{adrs_block}

# ACTIVE DECISIONS / SUPERSEDES / CONTRACTS / GAPS (from task event log)
{events_block}

# ACCEPTANCE CRITERIA (from plan — every subtask under review)
{accept_block}

# DECLARED DELIVERABLE TARGETS (the real outputs/paths the plan ordered — anchor on these)
{targets_block}

# DETERMINISTIC CHECK RESULTS (machine-verifiable; failures already certain)
{det_block}

# DELIVERABLES UNDER REVIEW (file path → content — FULL body unless a [[REVIEW-TRUNCATION: …]] marker appears)
{deliverables_block}

{verification_block}

# ORCHESTRATOR-VERIFIED SIDE-EFFECTS (read directly from okuro.db / git — AUTHORITATIVE)
The orchestrator inspected side effects you CANNOT see in the deliverable text above (okuro-memory writes, git commits) and captured them below. Treat these as ground truth: if an acceptance criterion asserts a side effect (e.g. "ADR written to okuro memory", "committed to git") and a matching entry appears here, that criterion is PROVEN — do NOT flag it as unproven for lack of inline evidence. Only flag a side-effect AC ``load_bearing`` when NO matching entry appears here.
{side_effect_block}

# WHAT TO LOOK FOR (non-exhaustive — be HIGH-RECALL)
- Drift from ADRs (the deliverable mentions a SUPERSEDED choice — old library, old approach, old name)
- Inconsistency across deliverables (matrix says X, architecture says Y, HTML shows Z)
- Broken cross-references (file:line citations that don't resolve, ADR-N tags pointing nowhere)
- Schema / contract violations (declared output_type doesn't match deliverable shape)
- Secret / config naming inconsistency
- Premature shipping language ("ready to ship", "good to go") when load-bearing gaps remain
- Open questions that were marked closed but have no resolution recorded
- Hallucinated specifics (URLs, paths, line numbers that look plausible but don't exist)

{ac_instructions}

# WHAT IS NOT A FAILURE (do not flag these — they are SUCCESS signals)
- `gap` events emitted by the producing subtask itself. These are honest escalations of missing prerequisites (user input, hardware tests, license rulings) — emitting them is the subagent doing its job per P0-2. Treat as "tracked", not "load-bearing failure", unless the deliverable ALSO claims the gap is resolved.
- `open_question` events emitted by the producing subtask. Same rationale — the subagent is surfacing an unknown for the user / next phase to resolve.
- Multiple subagents in a PARALLEL research phase disagreeing on the same decision axis. Divergence is by design — convergence is the synthesis phase's contract, not the research phase's.
- Subtask-internal contradictions are still load-bearing. Only the subagent-emitted `gap` / `open_question` events themselves get the "tracked" benefit-of-the-doubt.
- Content beyond a `[[REVIEW-TRUNCATION: …]]` marker. The deliverable was truncated to fit the review window — you are seeing a FRAGMENT, not the whole artifact. Do NOT flag a section as "missing", "undefined", or "incomplete" when it may live past the marker (e.g. a recommendation references §N and you don't see §N — it was likely truncated, not omitted). If the visible portion is genuinely insufficient to judge a criterion, emit at most a `cosmetic` finding ("deliverable exceeds review window"), NEVER `load_bearing`.

# OUTPUT FORMAT — emit ONLY a JSON object, nothing else (no prose, no commentary, no markdown fences)

{{
  "findings": [
    {{
      "severity": "load_bearing" | "cosmetic",
      "subtask_id": "<id of the subtask this finding concerns, e.g. \"1.2\" — copy it from the `### subtask X.Y` heading in DELIVERABLES UNDER REVIEW / ACCEPTANCE CRITERIA / TARGET PATHS that owns the deliverable you are flagging; null only if the finding spans the whole phase and no single subtask owns it>",
      "file": "<absolute or repo-relative path of the deliverable>",
      "line": <integer line number, or null if not line-specific>,
      "evidence": "<short verbatim quote or paraphrase of what is wrong>",
      "summary": "<one-sentence description of the issue>",
      "suggested_fix": "<concrete action — what to change, where>"
    }}
  ]
}}

Rules:
- ``load_bearing`` = the actual work is WRONG, MISSING, or there is NO evidence it was done at all. ``cosmetic`` = everything else, including how the evidence is PRESENTED.
- CRITICAL — do NOT mark evidence FORMATTING as load_bearing. If the deliverable demonstrably accomplishes the task, then the *shape* of the evidence is at most ``cosmetic``: wrong code-fence language (```text vs ```bash), a missing byte-count/exit-code line, "verbatim"/wording quibbles, prose alongside real output, nested fences — these are ``cosmetic``. Reserve ``load_bearing`` for: the output is substantively incorrect, the task was not actually done, or the artifact shows NO proof the work happened.
- CONVERGENCE — if this is a re-review (the deliverable was revised after prior findings) and those prior findings are now addressed and the work is substantively complete, return ``{{"findings": []}}`` (PASS). Do NOT hunt for new cosmetic nitpicks to keep failing it. A new round must only re-FAIL on a genuine, previously-unraised substantive defect.
- Cite the actual file + line whenever possible. If the issue spans files, emit one finding per (file, line).
- ATTRIBUTION — set ``subtask_id`` to the subtask that OWNS the flagged deliverable (the `### subtask X.Y` heading whose `brain://`/path the finding is about). A retry re-dispatches ONLY the named subtask, so a wrong id sends the fix to the wrong author. Use null ONLY for a genuine cross-subtask/whole-phase concern owned by no single subtask.
- If you find nothing, emit ``{{"findings": []}}`` — do not add prose.
- The Scorer reads your output. Be precise; ambiguous evidence wastes its budget.
"""


def _format_adrs(adrs: list[dict]) -> str:
    if not adrs:
        return "_(none — no user-locked decisions)_"
    lines = []
    for i, a in enumerate(adrs, 1):
        prompt = (a.get("prompt") or "?").strip()
        choice = (a.get("selected_label") or a.get("selected_option_id") or "?").strip()
        rationale = (a.get("selected_rationale") or "").strip()
        rejected = []
        for opt in (a.get("options") or []):
            label = (opt.get("label") or opt.get("id") or "").strip()
            if label and label != choice:
                rejected.append(label)
        rj = f" (rejected: {', '.join(rejected[:5])})" if rejected else ""
        lines.append(f"{i}. **{prompt}** → `{choice}`{rj}" + (f" — {rationale}" if rationale else ""))
    return "\n".join(lines)


def _format_events(events: list[dict]) -> str:
    if not events:
        return "_(no events in log)_"
    keep = {"decision", "supersedes", "contract", "gap", "open_question"}
    rows: list[str] = []
    for e in events:
        et = e.get("event_type")
        if et not in keep:
            continue
        seq = e.get("seq", "?")
        sub = e.get("subtask_id", "?")
        body = e.get("body") or {}
        body_str = ", ".join(f"{k}={v!r}"[:160] for k, v in body.items() if k != "schema_body")
        rows.append(f"- seq={seq} type={et} subtask={sub} :: {body_str}")
    return "\n".join(rows) or "_(no decisions / supersedes / contracts / gaps)_"


def _has_declared_acs(phase: Any) -> bool:
    """True iff ANY subtask under review declared acceptance criteria.

    When False, the AC-verification instructions must NOT be rendered —
    otherwise the Critic invents ACs (and external requirements like
    durability/persistence the plan never stated). See FIX B-2.
    """
    for st in getattr(phase, "subtasks", []) or []:
        if getattr(st, "acceptance_criteria", None):
            return True
    return False


def _format_acceptance(phase: Any) -> str:
    lines: list[str] = []
    for st in getattr(phase, "subtasks", []) or []:
        ac = getattr(st, "acceptance_criteria", None) or []
        if not ac:
            continue
        lines.append(f"### subtask {getattr(st, 'id', '?')} ({getattr(st, 'role', '?')})")
        for c in ac:
            lines.append(f"- [ ] {c}")
    return "\n".join(lines) or "_(no acceptance criteria declared)_"


def _format_targets(phase: Any) -> str:
    """Render the subtasks' declared target_paths + typed outputs so the
    Critic anchors on the REAL ordered deliverable instead of guessing
    (and inventing requirements the plan never stated)."""
    lines: list[str] = []
    for st in getattr(phase, "subtasks", []) or []:
        paths = getattr(st, "target_paths", None) or []
        outs = getattr(st, "outputs", None) or []
        if not paths and not outs:
            continue
        lines.append(f"### subtask {getattr(st, 'id', '?')} ({getattr(st, 'role', '?')})")
        for p in paths:
            lines.append(f"- path: `{p}`")
        for o in outs:
            lines.append(f"- output: {o}")
    return "\n".join(lines) or "_(no target paths / typed outputs declared)_"


# AC-verification protocol — rendered ONLY when ACs were actually declared.
_AC_INSTRUCTIONS_DECLARED = """# ACCEPTANCE CRITERIA NOT SATISFIED — verify each
- Acceptance criteria not satisfied (each subtask declared criteria — verify each)

# HOW TO VERIFY ACCEPTANCE CRITERIA (Gate 2 §C11 protocol)
For EVERY acceptance criterion listed under ACCEPTANCE CRITERIA above, scan the ARTIFACT BODIES under DELIVERABLES UNDER REVIEW (keys prefixed `brain://`) for captured evidence. The contract the subagent received tells it to attach captured stdout / exit-code in a fenced `bash` block per AC. Specifically look for:
- A fenced `bash` block (triple-backtick bash ... triple-backtick) containing the actual command + its stdout / exit code
- A line or heading that ties the evidence back to the AC (e.g. "## AC1 — <criterion>", "AC#1:", "AC 1 ✓")
- Numeric thresholds satisfied with measured values, not asserted ones

For ACs that assert a SIDE EFFECT (something written to okuro memory, committed to git, or otherwise persisted OUTSIDE the deliverable text): consult the ORCHESTRATOR-VERIFIED SIDE-EFFECTS block above, NOT the deliverable body. A matching memory write / commit there PROVES the AC — the subagent is not required to paste the write_memory() confirmation into the artifact. Flag such an AC ``load_bearing`` ONLY when that block shows no matching entry.

Severity calibration for ACs:
- ``load_bearing`` ONLY when an AC is genuinely UNPROVEN — there is no captured output, no measured value, nothing showing the criterion was met (and no `gap` event with `affects=[AC#N]`, and no matching ORCHESTRATOR-VERIFIED side-effect). Bare prose claims with zero supporting output count as unproven.
- If the AC IS demonstrated (the command output / measured value is present) but the evidence is imperfectly FORMATTED — wrong fence language, missing exit-code line, missing byte-count, partial wording — that is ``cosmetic``, NOT load_bearing. Do not block demonstrably-completed work over evidence presentation."""

# Rendered when the plan declared NO acceptance criteria. Anchors the Critic
# on the task/subtask description + the deliverable's existence & correctness,
# and explicitly forbids inventing ACs or external requirements.
_AC_INSTRUCTIONS_NONE = """# NO ACCEPTANCE CRITERIA DECLARED — judge by description + deliverable only
This phase declared NO acceptance criteria. Judge the deliverable ONLY against:
(a) the TASK / subtask DESCRIPTION above, and
(b) the existence and correctness of the DECLARED DELIVERABLE TARGETS (paths / outputs) under DELIVERABLES UNDER REVIEW.

DO NOT invent acceptance criteria. DO NOT introduce requirements the task did not state — including (but not limited to) durability, persistence, file-location/runtime guarantees, or temporal/longevity expectations. There is no "AC1"/"AC#N" to verify here; do not reference any. If the declared deliverable exists and is correct per the description, that is sufficient — flag a load_bearing finding ONLY for an actual defect in what was asked, not for a property the task never required."""

# Rendered for a `plan` / `report` deliverable (kind-aware review). A design /
# research artifact cannot produce captured shell output, so the executable
# bar above (a `bash` block + stdout per AC) would FAIL it forever. Judge ACs
# against the body's reasoning and coverage instead.
_AC_INSTRUCTIONS_CONCEPTUAL = """# ACCEPTANCE CRITERIA — judge CONCEPTUALLY (design / research deliverable)
This deliverable is a PLAN or REPORT, NOT executable code. Verify each acceptance criterion against the REASONING and COVERAGE in the deliverable body — NOT against captured shell output. Do NOT demand a fenced `bash` block, stdout, exit codes, or measured runtime values; this artifact kind cannot and must not produce them.

For EVERY acceptance criterion: confirm the body substantively ADDRESSES it — the decision is made, the section exists, the trade-off is analysed, the option is covered, the question the AC posed is answered.

Severity calibration:
- ``load_bearing`` ONLY when the body fails to address a criterion AT ALL — a missing required section, an unanswered required question, a decision left open that the AC required closed.
- Imperfect prose, missing citations, absent shell evidence, or stylistic gaps are ``cosmetic`` at most. Do NOT block a substantively-complete design document over evidence presentation.
- DO NOT invent acceptance criteria or external requirements the task never stated."""


def _format_det(det_results: list[Any]) -> str:
    rows: list[str] = []
    for r in det_results:
        # accept both CheckResult objects and dicts
        kind = getattr(getattr(r, "check", None), "kind", None) or (r.get("kind") if isinstance(r, dict) else "?")
        sev = getattr(getattr(r, "check", None), "severity", None) or (r.get("severity") if isinstance(r, dict) else "?")
        passed = getattr(r, "passed", None) if not isinstance(r, dict) else r.get("passed")
        findings = getattr(r, "findings", None) if not isinstance(r, dict) else r.get("findings")
        findings = findings or []
        if passed and not findings:
            continue
        for f in findings[:50]:
            rows.append(
                f"- [{kind}/{sev}] {f.get('file', '?')}:{f.get('line', '?')} — "
                f"{f.get('evidence', '')[:200]} → {f.get('suggested_fix', '')[:200]}"
            )
    return "\n".join(rows) or "_(deterministic stage clean)_"


def _format_deliverables(deliverable_text: dict[str, str]) -> str:
    if not deliverable_text:
        return "_(no deliverables to inspect)_"
    parts: list[str] = []
    for path, body in deliverable_text.items():
        parts.append(f"### `{path}`\n```\n{body}\n```")
    return "\n\n".join(parts)


def _format_prior_findings(prior_findings: Optional[list]) -> str:
    """Render the stateful verification block — the prior round's findings the
    Critic MUST verify, one by one, instead of re-deriving a fresh set.

    This is the wiring that the May-31 "converge re-reviews" prompt rule
    (CONVERGENCE, below) always assumed but never received: the Critic was told
    "pass if prior findings are resolved" while never being shown what they
    were. Empty/None → "" (first review; nothing to verify)."""
    findings = [f for f in (prior_findings or []) if isinstance(f, dict)]
    if not findings:
        return ""
    ordered = sorted(
        findings, key=lambda f: 0 if f.get("severity") == "load_bearing" else 1,
    )
    lines = [
        "# PRIOR-ROUND FINDINGS — VERIFY EACH (THIS IS A RE-REVIEW)",
        "The previous review raised the findings below and the deliverable was "
        "revised to address them. This is a VERIFICATION pass, not a fresh hunt. "
        "For EACH finding decide, from the deliverable above, RESOLVED or "
        "STILL-OPEN:",
        "- RESOLVED → do NOT re-flag it.",
        "- STILL-OPEN → re-flag it (same severity), citing exactly what is still "
        "wrong.",
        "You may add a NEW finding ONLY if it is a genuine, distinct, "
        "previously-unraised load_bearing defect — NOT a restatement of any item "
        "below, and NOT a fresh cosmetic nitpick. If every load_bearing item "
        "below is resolved and no new substantive defect exists, return "
        '``{{"findings": []}}`` (PASS).',
        "",
        "Prior findings:",
    ]
    for i, f in enumerate(ordered, 1):
        loc = f"{f.get('file', '?')}:{f.get('line', '?')}"
        lines.append(
            f"{i}. [{f.get('severity', '?')}] {loc} — "
            f"{str(f.get('summary', ''))[:240]}"
        )
    return "\n".join(lines)


def _build_prompt(
    *,
    task: Any,
    phase: Any,
    adrs: list[dict],
    events: list[dict],
    deterministic_results: list[Any],
    deliverable_text: dict[str, str],
    ac_mode: str = "auto",
    prior_findings: Optional[list] = None,
) -> str:
    # ``ac_mode`` is the review-profile's critic_ac_mode. "conceptual" judges a
    # plan/report against its reasoning (no shell-evidence bar); "auto" keeps
    # the legacy declared/none binary for code.
    if ac_mode == "conceptual":
        ac_instructions = _AC_INSTRUCTIONS_CONCEPTUAL
    elif _has_declared_acs(phase):
        ac_instructions = _AC_INSTRUCTIONS_DECLARED
    else:
        ac_instructions = _AC_INSTRUCTIONS_NONE
    # Pre-verify side-effect ACs (memory writes / git commits) the Critic
    # cannot observe in the deliverable text. Best-effort — never let a
    # lookup failure crash prompt assembly.
    try:
        from okuro.orchestrator.reviewer.side_effect_evidence import (
            build_side_effect_evidence,
        )
        side_effect_block = build_side_effect_evidence(task, phase)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("critic: side-effect evidence lookup failed: %s", exc)
        side_effect_block = "_(side-effect verification unavailable)_"
    return _PROMPT_TEMPLATE.format(
        task_id=getattr(task, "id", "?"),
        task_description=(getattr(task, "description", "") or "")[:400],
        phase_id=getattr(phase, "id", "?"),
        phase_name=getattr(phase, "name", "?"),
        adrs_block=_format_adrs(adrs),
        events_block=_format_events(events),
        accept_block=_format_acceptance(phase),
        targets_block=_format_targets(phase),
        det_block=_format_det(deterministic_results),
        deliverables_block=_format_deliverables(deliverable_text),
        verification_block=_format_prior_findings(prior_findings),
        ac_instructions=ac_instructions,
        side_effect_block=side_effect_block,
    )


_JSON_BLOCK_RE = re.compile(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", re.DOTALL)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


_PARSE_FAILURE_SENTINEL = "__critic_parse_failure__"


def _extract_balanced_object(raw: str) -> Optional[str]:
    """Return the substring from the first ``{`` to its matching ``}``.

    Honors brace nesting and braces inside JSON string values (e.g. an
    ``evidence`` field that itself quotes ``{speechAPIExists: true}``). The
    simple ``_JSON_BLOCK_RE`` only balances one level and silently fails on
    such payloads, which is what drove parse-failure FALSE FAILs. Returns
    None when there is no balanced object.
    """
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return None


def _parse_findings(raw: str) -> list[dict]:
    """Extract findings list from bridge output. Tolerant to fences / preamble.

    On hard parse failure we return a single finding flagged with the
    `_PARSE_FAILURE_SENTINEL` summary marker. `run_critic` lifts that to
    `infra_error=True` so the pipeline never burns a user-subagent retry
    on a reviewer-stage parser blip — the Critic LLM emitting malformed
    JSON is a transport-class issue, not a real content fault.
    """
    if not raw:
        return []
    candidates: list[str] = []
    # 1. Fenced payload(s) — the model frequently emits a chatty preamble
    #    ("Now I have all I need. Producing the JSON critic output:") then a
    #    ```json ... ``` block. Pull the fenced object so the preamble never
    #    reaches json.loads (the old code only stripped the fence MARKERS and
    #    left the preamble, guaranteeing a parse miss → false FAIL).
    candidates.extend(m.group(1) for m in _FENCED_JSON_RE.finditer(raw))
    # 2. Whole string with surrounding fence markers stripped.
    candidates.append(
        re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    )
    # 3. Raw, unmodified.
    candidates.append(raw)
    # 4. Brace-balanced slice from the first ``{`` — survives nested braces
    #    in evidence strings.
    balanced = _extract_balanced_object(raw)
    if balanced:
        candidates.append(balanced)
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
            return [_normalize_finding(f) for f in obj["findings"] if isinstance(f, dict)]
    # Fallback: scan for the first one-level JSON object with a findings key.
    for m in _JSON_BLOCK_RE.finditer(raw):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
                return [_normalize_finding(f) for f in obj["findings"] if isinstance(f, dict)]
        except json.JSONDecodeError:
            continue
    # Hard fallback — flag with the sentinel so run_critic surfaces this
    # as infra_error. Verdict stays FAIL but no user-subagent retry burns.
    log.warning("critic: failed to parse JSON output (%d chars); flagging as infra_error", len(raw))
    return [{
        "severity": "load_bearing",
        "file": None,
        "line": None,
        "evidence": raw[:400],
        "summary": _PARSE_FAILURE_SENTINEL,
        "suggested_fix": "Re-run critic with stricter prompt or inspect raw output manually.",
    }]


def _normalize_finding(f: dict) -> dict:
    sev = str(f.get("severity") or "").lower().strip()
    if sev not in {"load_bearing", "cosmetic"}:
        # C2 — map common LLM severity SYNONYMS before the cosmetic fallback. A
        # critic that emits "critical"/"major"/"high"/"blocker" clearly means a
        # blocking defect; the old code silently demoted ALL non-canonical
        # severities to cosmetic, so a mis-tagged load-bearing finding never
        # forced FAIL (false-PASS — could ship broken work).
        if sev in {"critical", "major", "high", "blocker", "blocking",
                   "severe", "fatal", "error"}:
            sev = "load_bearing"
        else:
            # Genuinely unparseable / minor severity → cosmetic. Defaulting
            # unknown UP (recall bias) turned every mis-tagged finding into a
            # FAIL-forcing blocker, driving the observed review churn (5 rounds
            # of evidence-format nitpicks on trivial subtasks). Only KNOWN
            # load-bearing synonyms escalate; the truly-ambiguous stay cosmetic.
            sev = "cosmetic"
    # P3 (#9) — preserve the owning subtask id so rerun_subtasks_from_verdict
    # (pipeline.py:788) routes the retry to the RIGHT subtask instead of the
    # last-done guess. Normalize "" / missing to None (a falsy id must not
    # register as an owner). Coerced to str so a numeric "1" / 1 both match
    # the subtask id.
    _sid = f.get("subtask_id")
    _sid = str(_sid).strip() if _sid not in (None, "") else None
    return {
        "severity": sev,
        "subtask_id": _sid,
        "file": f.get("file"),
        "line": f.get("line"),
        "evidence": (f.get("evidence") or "")[:600],
        "summary": (f.get("summary") or "")[:400],
        "suggested_fix": (f.get("suggested_fix") or "")[:600],
    }


def run_critic(
    *,
    task: Any,
    phase: Any,
    adrs: list[dict],
    events: list[dict],
    deterministic_results: list[Any],
    deliverable_text: dict[str, str],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 600,
    max_attempts: int = 2,
    tasks_dir: Optional[Any] = None,
    ac_mode: str = "auto",
    prior_findings: Optional[list] = None,
) -> dict:
    """Invoke the Critic via bridge. Returns ``{findings, raw, ok, error, infra_error}``.

    Timeout default is 600s (10min) — phase deliverables can exceed 20KB by
    phase 5/6, and Sonnet needs headroom to enumerate findings. Retries
    once on bridge-side failure (timeout, connection error) before
    surfacing the synthetic FAIL finding, so transient infra blips don't
    burn a user-subagent retry cycle.
    """
    prompt = _build_prompt(
        task=task,
        phase=phase,
        adrs=adrs,
        events=events,
        ac_mode=ac_mode,
        deterministic_results=deterministic_results,
        deliverable_text=deliverable_text,
        prior_findings=prior_findings,
    )
    from okuro.bridge.invoke import invoke as bridge_invoke
    from pathlib import Path as _Path

    # Stream the Critic's thinking into the task's .activity.jsonl so the
    # UI activity feed shows live progress during the 2-5 min critic call.
    # Pre-fix: bridge_invoke ran a non-streaming subprocess.run; the feed
    # sat silent until the entire LLM call returned. Now the
    # `_normalize_event_claude` parser projects each stream-json event into
    # an activity row tagged with subtask_id='reviewer:phase{N}:critic'
    # + role='critic'. Only the claude provider supports this; other
    # providers fall back to the non-streaming path.
    task_id_for_sink = getattr(task, "id", "") or ""
    phase_id_for_sink = getattr(phase, "id", "") or ""
    activity_sink: _Path | None = None
    sink_subtask = ""
    if task_id_for_sink:
        try:
            # WP7 — prefer the engine-threaded tasks_dir so the activity
            # sink lands beside the engine's own log under a non-default
            # --config. Fall back to load_config() only when unthreaded.
            if tasks_dir is not None:
                _base = _Path(tasks_dir)
            else:
                from okuro.orchestrator.config import load_config
                _base = load_config().tasks_dir
            activity_sink = _base / task_id_for_sink / ".activity.jsonl"
            sink_subtask = f"reviewer:phase{phase_id_for_sink}:critic"
        except Exception:
            activity_sink = None

    last_error = "bridge_invoke failed"
    for attempt in range(1, max_attempts + 1):
        _prov = provider or "claude"
        res = bridge_invoke(
            prompt=prompt,
            capability=None,
            provider=_prov,
            # Reviewer model default. On CLAUDE the cheap "haiku" default is the
            # dominant review-cost lever (structured eval, ~4 rounds/subtask).
            # On a NON-claude provider (codex/openai) fall back to "" so its own
            # CLI picks the account-appropriate default model — never a claude
            # literal (lets `preferred_cli=codex` review on the openai model).
            model=model or ("haiku" if _prov == "claude" else ""),
            timeout=timeout,
            activity_sink=activity_sink,
            subtask_id=sink_subtask or None,
            role="critic",
        )
        if res.get("success"):
            raw = (res.get("output") or "").strip()
            findings = _parse_findings(raw)
            # Parser sentinel = transport-class failure, not content. Lift to
            # infra_error so rerun_subtasks_from_verdict returns [] and the
            # engine doesn't spend a retry slot on a Critic JSON parse glitch.
            parse_failed = any(
                (f or {}).get("summary") == _PARSE_FAILURE_SENTINEL
                for f in findings
            )
            if parse_failed:
                # Replace sentinel with the human-readable summary so audit
                # rows + verdict events read clearly.
                for f in findings:
                    if f.get("summary") == _PARSE_FAILURE_SENTINEL:
                        f["summary"] = "Critic output was not valid JSON; reviewer-stage parser failure."
                return {"ok": False, "raw": raw, "error": "critic parse failure",
                        "findings": findings, "infra_error": True}
            return {"ok": True, "raw": raw, "error": "", "findings": findings,
                    "infra_error": False}
        last_error = res.get("error") or "bridge_invoke failed"
        log.warning("critic: bridge attempt %d/%d failed: %s",
                    attempt, max_attempts, last_error)

    # All attempts failed — surface an infra error distinct from content
    # findings so the pipeline can choose NOT to burn a user-subagent
    # retry on a reviewer-stage blip.
    return {
        "ok": False,
        "raw": "",
        "error": last_error,
        "infra_error": True,
        "findings": [{
            "severity": "load_bearing",
            "file": None,
            "line": None,
            "evidence": f"Critic bridge call failed: {last_error[:200]}",
            "summary": "Critic stage could not run — treat as FAIL until reviewer is rerun.",
            "suggested_fix": "Retry critic stage; if bridge is unavailable, fall back to human review.",
        }],
    }
