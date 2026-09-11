# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: task decomposition using LLM orchestration
# index:
#   imports
#   def try_micro_decompose
#   def decompose_task
#   def _plan_with_project_retry
#   def _load_project_slugs
#   def _classify_project_slug
#   def _suggest_similar_slugs
#   def _assert_project_resolvable
#   def call_llm_api
#   def call_llm
#   def parse_plan
#   def validate_plan
#   def plan_to_phases
#   def decompose_continuation
#   def format_role_index
#   def _get_role_description
# AGENT_HEADER_END -->
"""Okuro Orchestrator Task Decomposition — breaks tasks into executable subtasks."""

from pathlib import Path
from typing import Optional
import json as _json
import subprocess
import yaml
import re
import os
import logging
from datetime import datetime as _dt

from okuro.orchestrator.config import Config, get_cli_tool_config, load_role_index
from okuro.orchestrator.state import (
    Phase, Subtask, DecisionGate, DecisionGateOption,
)
from okuro.llm_hygiene import strip_bootstrap_greeting
from okuro.orchestrator.write_intent import (
    has_write_intent as _has_write_intent,
    undeclared_write_intent as _undeclared_write_intent,
)
from okuro.orchestrator.yamlfast import yload

logger = logging.getLogger("okuro.orchestrator.decomposer")


# ---------------------------------------------------------------------------
# PR A — decomposer structural milestone emissions. The decomposer LLM
# call already streams tokens into ``.activity.jsonl`` via bridge.invoke
# when task_id is provided (see ``call_llm`` below). What was missing:
# the *structural* steps around that call (read brief, load role index,
# load capability registry, validate plan) produced zero events, so the
# FE decomposer-activity-panel rendered empty for 30–60s before the LLM
# tokens started flowing. This helper writes one row per milestone with
# subtask_id="decomposer:<task_id>" so the FE filter picks them up.
# ---------------------------------------------------------------------------

def _emit_decomposer_milestone(config: Config, task_id: Optional[str],
                                label: str, **fields) -> None:
    """Append a structural milestone row to <task_id>/.activity.jsonl.

    Best-effort. Silent on missing task_id (legacy CLI / test paths) or
    any filesystem failure — never raise on the decomposer hot path.
    """
    if not task_id:
        return
    try:
        path = config.tasks_dir / task_id / ".activity.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "type": "thinking",
            "subtask_id": f"decomposer:{task_id}",
            "role": "decomposer",
            "ts": _dt.utcnow().isoformat(),
            "preview": label,
        }
        row.update(fields)
        with open(path, "a") as fh:
            fh.write(_json.dumps(row) + "\n")
    except Exception as exc:
        logger.warning("decomposer milestone emit failed: %s", exc)

COMPLEX_KEYWORDS = {
    "architect", "design", "refactor", "migrate", "redesign",
    "build from scratch", "implement system", "create framework",
    "evaluate alternatives", "security audit", "performance analysis",
}

MICRO_PROMPT_TEMPLATE = """You are a task planner. This is a simple operational task that needs a minimal execution plan.

Task: {task_description}

Output ONLY a YAML block with a single phase and 1-2 subtasks. Keep it minimal.
Available roles: {roles}

```yaml
phases:
  - id: 1
    name: "Execute"
    subtasks:
      - id: "1.1"
        role: <best-fit-role>
        description: "<what to do>"
        artifact_name: "<short-kebab-case-label>"
        risk: <LOW|MED|HIGH>
        complexity: fast
        dependencies: []
```

Output your YAML plan:"""


def _load_registered_capability_ids(config: Config) -> set[str]:
    """Wave-3 G6 helper — load capability ids from the registry.

    Returns an empty set when the registry is unavailable / empty so the
    caller can pass it unconditionally to validate_plan (which interprets
    empty set differently from None: empty = registry consulted, none found;
    None = registry not consulted at all).
    """
    try:
        from okuro.orchestrator.capabilities.registry import CapabilityRegistry
        cap_path = config.orchestrator_root / "capabilities.yaml"
        registry = CapabilityRegistry(cap_path)
        # Registry stores Capability objects keyed by id; expose ids only.
        ids = getattr(registry, "ids", None)
        if ids is None:
            # Fall back to introspecting the underlying store. Different
            # registry implementations expose either .all() returning a
            # list or .capabilities dict — try both safely.
            caps = getattr(registry, "capabilities", None)
            if isinstance(caps, dict):
                return set(caps.keys())
            try:
                return {c.id for c in registry.all()}
            except Exception:
                return set()
        return set(ids)
    except Exception:
        return set()


def try_micro_decompose(
    task_description: str, config: Config, task_id: str | None = None,
) -> Optional[list]:
    """Attempt micro-decomposition for simple tasks.
    Returns list of Phase objects if simple enough, else None.

    ``task_id`` — when provided, the LLM call streams tokens into the
    task's ``.activity.jsonl`` via ``execute_streaming`` so the activity
    feed lights up during the decompose call. Without it, the legacy
    blocking subprocess.run path is used (tests, callers with no task).
    """
    words = task_description.lower().split()
    if len(words) > 300:
        return None
    for keyword in COMPLEX_KEYWORDS:
        if keyword in task_description.lower():
            return None

    logger.info(f"[MICRO] Task looks simple ({len(words)} words), using micro-decomposition")

    role_index = load_role_index()
    role_names = ", ".join(sorted(role_index.keys()))

    prompt = MICRO_PROMPT_TEMPLATE.format(
        task_description=task_description, roles=role_names,
    )

    try:
        # Pass task_id only when set — preserves the 2-positional call_llm
        # signature used by some test monkeypatches.
        if task_id:
            response = call_llm(prompt, config, task_id=task_id)
        else:
            response = call_llm(prompt, config)
        plan_dict = parse_plan(response)

        phases = plan_dict.get("phases", [])
        if len(phases) > 1:
            logger.info("[MICRO] LLM returned multi-phase plan, falling back")
            return None

        total_subtasks = sum(len(p.get("subtasks", [])) for p in phases)
        if total_subtasks > 3:
            logger.info("[MICRO] Too many subtasks, falling back")
            return None

        # The micro planner has no retry loop, so a violation here falls back
        # to the strategic planner rather than being refused outright — which
        # then re-prompts once with the violation named. This is the path the
        # LV-4 exposure came down: one subtask, straight to running, no gate.
        _violations = _write_intent_violations(plan_dict)
        if _violations:
            logger.info(
                "[MICRO] write intent undeclared (%s) — falling back to the "
                "strategic planner", "; ".join(_violations)[:200],
            )
            return None

        warnings = validate_plan(
            plan_dict, role_index,
            registered_capabilities=_load_registered_capability_ids(config),
        )
        if warnings:
            for w in warnings:
                logger.warning(f"[MICRO] {w}")

        return plan_to_phases(plan_dict)
    except Exception as e:
        logger.warning(f"[MICRO] Micro-decomposition failed: {e}, falling back")
        return None


def decompose_task(
    task_description: str,
    config: Config,
    required_roles: list[str] | None = None,
    intelligence: str = "",
    task_id: str | None = None,
) -> list[Phase]:
    """Decompose a task into phases and subtasks using LLM orchestration.

    Args:
        task_description: Human-readable task description
        config: Okuro orchestrator configuration
        required_roles: Roles that MUST appear in ≥1 subtask
        intelligence: "" (default tiers) or "max" (force strategic planner, skip micro)
        task_id: when provided, the decompose LLM call streams tokens into
            ``<tasks_dir>/<task_id>/.activity.jsonl`` via
            ``execute_streaming`` (Critic/Scorer pattern). Synthetic
            ``subtask_id="decomposer:<task_id>"`` + ``role="decomposer"``
            tag every emitted row so the FE activity feed groups them
            under a dedicated card during the 3–5 min decompose call.
            Falls back to the blocking subprocess.run path when omitted.
    """
    _emit_decomposer_milestone(
        config, task_id,
        label=f"Decomposing task (intelligence={intelligence or 'default'}).",
        stage="enter",
    )

    if intelligence == "max":
        logger.info("[DECOMPOSE] intelligence=max → skipping micro-decompose, using strategic planner")
    else:
        micro_result = try_micro_decompose(task_description, config, task_id=task_id)
        if micro_result is not None:
            _emit_decomposer_milestone(
                config, task_id,
                label="Plan resolved by micro-decomposer (short task).",
                stage="micro_resolved",
            )
            return micro_result

    _emit_decomposer_milestone(
        config, task_id, label="Loading decompose prompt template.",
        stage="load_prompt",
    )
    prompt_path = config.prompts_dir / "decompose.md"
    with open(prompt_path, "r") as f:
        prompt_template = f.read()

    # Load principles (optional — may not exist in all deployments)
    principles_text = ""
    principles_path = config.knowledge_dir / "global" / "principles.md"
    if principles_path.exists():
        with open(principles_path, "r") as f:
            principles_text = f.read()

    _emit_decomposer_milestone(
        config, task_id, label="Loading role index.",
        stage="load_roles",
    )
    role_index = load_role_index()
    role_index_compact = format_role_index(role_index)

    # Load capability registry
    _emit_decomposer_milestone(
        config, task_id, label="Loading capability registry.",
        stage="load_capabilities",
    )
    capabilities_summary = ""
    try:
        from okuro.orchestrator.capabilities.registry import CapabilityRegistry
        cap_path = config.orchestrator_root / "capabilities.yaml"
        registry = CapabilityRegistry(cap_path)
        capabilities_summary = registry.format_for_prompt(max_caps=15)
    except Exception:
        pass

    filled_prompt = prompt_template.replace("{principles_text}", principles_text)
    filled_prompt = filled_prompt.replace("{role_index_compact}", role_index_compact)
    filled_prompt = filled_prompt.replace("{capabilities_summary}", capabilities_summary)
    filled_prompt = filled_prompt.replace("{task_description}", task_description)

    # Inject required_roles constraint if user specified roles.
    # Contract: every listed role must appear in ≥1 subtask. If no
    # natural fit exists, generate a lens-analysis subtask where the
    # role investigates the task from its own perspective.
    # M1.5 — intelligence=max routes through gates by default. Teach the
    # planner to emit a `decision_gate` on any phase that contains a
    # load-bearing architectural choice (stack pick, transport, lib,
    # storage backend). Gates pause the engine until the user picks an
    # option; the choice is then injected as an ADR into every downstream
    # subagent brief, so wrong picks waste minutes — not hours.
    if intelligence == "max":
        gate_rider = (
            "\n\n## Decision Gates (intelligence=max)\n\n"
            "If — and only if — a phase contains a **load-bearing architectural "
            "choice** (stack pick, transport / protocol, animation or UI lib, "
            "storage backend, deployment target, language/runtime, framework), "
            "attach a `decision_gate` to that phase. The engine will pause "
            "before dispatching subtasks of that phase and ask the user to "
            "pick. The locked choice is auto-injected as an ADR into every "
            "downstream subagent brief.\n\n"
            "**Skip gates for phases that contain only mechanical / "
            "execution work** (porting a file, writing a doc, running a "
            "command). Over-gating is a worse failure than under-gating — "
            "it stalls every task on trivia.\n\n"
            "Schema (add at the phase level alongside `id`, `name`, "
            "`subtasks`):\n\n"
            "```yaml\n"
            "    decision_gate:\n"
            "      id: gate-<short-slug>            # stable id, kebab-case\n"
            "      prompt: <one-line user question>\n"
            "      options:\n"
            "        - id: <option-slug>\n"
            "          label: <short human label>\n"
            "          description: <one-line summary>\n"
            "          pros: <comma-separated upsides>\n"
            "          cons: <comma-separated downsides>\n"
            "          risk: <one-word>\n"
            "          recommended: true   # at most one option\n"
            "        - id: ...\n"
            "          ...\n"
            "```\n\n"
            "Provide 2–4 options per gate. Mark exactly one as "
            "`recommended: true` based on the principles + user profile. "
            "A phase carrying a gate is auto-serialised — the dispatcher "
            "will not run its subtasks in parallel.\n"
        )
        filled_prompt += gate_rider

    if required_roles:
        roles_csv = ", ".join(required_roles)
        constraint = (
            f"\n\n## Required Roles (MANDATORY)\n\n"
            f"The user has committed the following roles to this task via a flow: "
            f"**{roles_csv}**.\n\n"
            f"**CONTRACT — every listed role MUST produce at least one subtask.** "
            f"For each role, decide what it should contribute given the task. If a "
            f"role has a clear fit (e.g. security-auditor on an auth task), assign "
            f"it normally.\n\n"
            f"**FALLBACK — lens analysis.** If you cannot identify a direct "
            f"contribution for a role, do NOT drop it. Instead, generate a "
            f"`complexity: standard` subtask where that role analyzes the task "
            f"from its own perspective — identifying risks, opportunities, "
            f"constraints, and contributions it sees. Artifact name: "
            f"`{{role}}-lens-analysis`. Description template: \"Analyze the task "
            f"from the {{role}} perspective. Identify risks, opportunities, "
            f"constraints, and concrete contributions this role can offer.\"\n\n"
            f"The plan is incomplete if any required role is missing. "
            f"You may add other roles beyond the required list as needed.\n"
        )
        filled_prompt += constraint

    # F3 — inject prior-flow forward guidance. The user rates a finished flow
    # and may leave a free-text note "for the next flow to consider"
    # (flow_feedback.comment). Surface the most-recent unresolved notes here so
    # they shape planning, then mark them resolved so each note lands in exactly
    # ONE future plan and never echoes again (bounded + non-repeating). Best-
    # effort: a DB hiccup must never break decomposition. The micro-decompose
    # path deliberately skips this — it bypasses all planning context by design.
    try:
        from okuro.orchestrator import feedback as _feedback_svc

        forward_notes = _feedback_svc.unresolved_forward_comments(limit=3)
        if forward_notes:
            lines = [
                "\n\n## Prior-flow guidance from the user\n\n"
                "The user left the following note(s) after earlier flows, to be "
                "considered when planning this one. Treat as soft guidance on "
                "approach / scope / pitfalls — not as hard requirements:\n"
            ]
            for note in forward_notes:
                oc = note.get("outcome_class") or "?"
                lines.append(f"- ({oc}) {str(note.get('comment') or '').strip()}")
            filled_prompt += "\n".join(lines) + "\n"
            _feedback_svc.mark_comments_resolved(
                [n["task_id"] for n in forward_notes]
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("decompose: forward-comment injection skipped: %r", exc)

    available_slugs = _load_project_slugs()
    _emit_decomposer_milestone(
        config, task_id, label="Invoking strategic planner LLM.",
        stage="llm_call",
    )
    plan_dict = _plan_with_project_retry(
        filled_prompt, config, intelligence, available_slugs,
        extra_validators=[_assert_write_intent_declared],
        task_id=task_id,
    )

    if required_roles:
        _ensure_required_roles_present(plan_dict, required_roles)

    _emit_decomposer_milestone(
        config, task_id, label="Validating plan (roles, dependencies, capabilities).",
        stage="validate",
    )
    warnings = validate_plan(
        plan_dict, role_index,
        registered_capabilities=_load_registered_capability_ids(config),
    )
    if warnings:
        print("Plan validation warnings:")
        for warning in warnings:
            print(f"  - {warning}")

    # Hard pre-flight: every assigned role MUST be resolvable to prompt
    # content NOW. validate_plan only checks that the role NAME is
    # registered (in role_index) — a role can be registered metadata-only
    # and still throw at dispatch time, leaving the task half-executed
    # and blocked on a `subtask_failed_permanently`. Failing here keeps
    # the failure cheap and the message actionable.
    _assert_roles_resolvable(plan_dict)

    phases = plan_to_phases(plan_dict)
    _emit_decomposer_milestone(
        config, task_id,
        label=f"Plan built — {len(phases)} phase(s) emitted.",
        stage="done",
        phase_count=len(phases),
    )
    return phases


def _plan_with_project_retry(
    prompt: str,
    config: Config,
    intelligence: str,
    available_slugs: set[str],
    extra_validators: "list | None" = None,
    max_retries: int = 1,
    task_id: str | None = None,
) -> dict:
    """Call planner; on validator rejection, retry once with feedback.

    Bounded loop — one retry is enough to convert a bad guess into a
    grounded choice when the rejection message names the candidates. More
    retries waste tokens and rarely converge.

    ``extra_validators`` is a list of callables ``validator(plan_dict)``
    that raise ``ValueError`` on rejection. Project-slug validation always
    runs first. Continuation flow adds a supersedes-target validator.
    """
    feedback: str | None = None
    last_err: ValueError | None = None
    for attempt in range(max_retries + 1):
        attempt_prompt = prompt
        if feedback is not None:
            attempt_prompt = (
                prompt
                + "\n\n## Prior Attempt Rejected — Fix and Re-emit\n\n"
                + feedback
                + "\n\nRe-run Phase 0 Orient before emitting. Pick exactly one of: "
                  "existing slug, `NEW:<slug>`, or `null` + `scope: system`.\n"
            )
        # Pass intelligence + task_id kwargs only when set — preserves the
        # 2-positional call_llm signature used by some test monkeypatches.
        kwargs = {}
        if intelligence:
            kwargs["intelligence"] = intelligence
        if task_id:
            kwargs["task_id"] = task_id
        if kwargs:
            response = call_llm(attempt_prompt, config, **kwargs)
        else:
            response = call_llm(attempt_prompt, config)
        if task_id:
            try:
                dump = config.tasks_dir / task_id / f".continuation-llm-response-attempt{attempt + 1}.txt"
                dump.parent.mkdir(parents=True, exist_ok=True)
                dump.write_text(response, encoding="utf-8")
            except Exception:
                pass
        plan_dict = parse_plan(response)
        try:
            _assert_project_resolvable(plan_dict, available_slugs)
            for validator in (extra_validators or []):
                validator(plan_dict)
            return plan_dict
        except ValueError as exc:
            last_err = exc
            feedback = str(exc)
            logger.warning(
                "[plan-validator] attempt %d/%d rejected: %s",
                attempt + 1, max_retries + 1, feedback,
            )
            if attempt == max_retries:
                break
    assert last_err is not None
    raise last_err


def _assert_supersedes_targets_valid(
    plan_dict: dict,
    supersedeable_subtask_ids: set[str],
    new_subtask_ids: set[str],
) -> None:
    """Validate every phase-level supersedes entry references an existing,
    supersedeable subtask (pending/approved).

    Raises ``ValueError`` with planner-readable feedback on first violation.
    Empty supersedes lists are fine — most continuations don't obsolete
    prior work; they extend it.
    """
    offenders: list[str] = []
    for phase in plan_dict.get("phases") or []:
        targets = phase.get("supersedes") or []
        for target in targets:
            target_str = str(target)
            if target_str in new_subtask_ids:
                offenders.append(
                    f"phase {phase.get('id', '?')} supersedes `{target_str}` "
                    f"which is one of the NEW subtasks — supersedes can only "
                    f"reference PRE-EXISTING pending/approved subtasks"
                )
                continue
            if target_str not in supersedeable_subtask_ids:
                offenders.append(
                    f"phase {phase.get('id', '?')} supersedes `{target_str}` "
                    f"which is not a supersedeable subtask (must be pending "
                    f"or approved, and exist in the prior plan)"
                )
    if offenders:
        valid_list = ", ".join(sorted(supersedeable_subtask_ids)) or "(none)"
        raise ValueError(
            "SUPERSEDES_INVALID: " + "; ".join(offenders)
            + f". Valid supersedes targets: {valid_list}. "
              "Drop the field entirely if your new phases don't obsolete "
              "prior pending work."
        )


def _runnable_role_catalog() -> list[tuple[str, str]]:
    """(role_id, searchable_blob) for every role with resolvable prompt content."""
    from okuro.orchestrator.config import resolve_role
    from okuro.roles.registry import list_roles

    out: list[tuple[str, str]] = []
    for r in list_roles():
        rid = r.get("id")
        if not rid:
            continue
        try:
            resolve_role(rid)
        except ValueError:
            continue
        blob = f"{rid} {r.get('domain', '')} {r.get('description', '')}".lower()
        out.append((rid, blob))
    return out


def _fallback_runnable_role(orig_role: str, description: str,
                            runnable: list[tuple[str, str]]) -> str | None:
    """Best runnable role for a subtask whose planned role is unrunnable.

    Deterministic keyword overlap (no LLM) over the runnable catalog; on a
    tie / no overlap, prefer a generic 'researcher' then the first runnable.
    """
    import re

    if not runnable:
        return None
    tokens = set(re.findall(r"[a-z]{3,}", f"{orig_role} {description}".lower()))
    best, best_score = None, -1
    for rid, blob in runnable:
        score = len(tokens & set(re.findall(r"[a-z]{3,}", blob)))
        if score > best_score:
            best, best_score = rid, score
    if best_score <= 0:
        ids = [rid for rid, _ in runnable]
        return "researcher" if "researcher" in ids else runnable[0][0]
    return best


def _assert_roles_resolvable(plan_dict: dict) -> None:
    """REMAP any subtask whose role has no prompt content to a runnable role.

    The decomposer LLM can pick a role that is in the index but has no prompt
    — a catalog YAML checked in empty (`vfx-specialist`, 2026-05-07), or a
    project-level agent name the planner knows but the orchestrator can't run
    (`caddy-web-maintainer`, surfaced LIVE 2026-06-19). Pre-fix this RAISED and
    the ENTIRE task hard-failed at planning with no recovery — a CEO just saw
    the task die. Now we remap each unrunnable role to the best runnable
    fallback (deterministic keyword match) and log it, so the task proceeds.
    Mutates plan_dict in place.
    """
    from okuro.orchestrator.config import resolve_role

    runnable: list[tuple[str, str]] | None = None
    remapped: list[str] = []
    for phase in plan_dict.get("phases") or []:
        for st in phase.get("subtasks") or []:
            role = st.get("role")
            if not role:
                continue
            try:
                resolve_role(role)
            except ValueError:
                if runnable is None:
                    runnable = _runnable_role_catalog()
                fb = _fallback_runnable_role(role, st.get("description", "") or "", runnable)
                if fb and fb != role:
                    remapped.append(f"{st.get('id', '?')}:{role}->{fb}")
                    st["role"] = fb

    if remapped:
        logger.warning(
            "decompose: remapped %d unrunnable role(s) to runnable fallbacks "
            "(graceful degradation, not a hard fail): %s",
            len(remapped), "; ".join(remapped),
        )


def _ensure_required_roles_present(plan_dict: dict, required_roles: list[str]) -> None:
    """Belt-and-braces: append lens-analysis subtasks for any required_role
    the LLM forgot to include. Mutates plan_dict in place.

    Only runs when required_roles is non-empty, so no-flow tasks are
    untouched.
    """
    phases = plan_dict.get("phases") or []
    if not phases:
        return

    used_roles: set[str] = set()
    for phase in phases:
        for st in phase.get("subtasks") or []:
            role = st.get("role")
            if role:
                used_roles.add(role)

    missing = [r for r in required_roles if r not in used_roles]
    if not missing:
        return

    last_phase = phases[-1]
    last_id = last_phase.get("id", len(phases))
    existing_subtasks = last_phase.setdefault("subtasks", [])
    next_seq = len(existing_subtasks) + 1

    logger.warning(
        "[required_roles] LLM omitted %d role(s); injecting lens-analysis fallbacks: %s",
        len(missing),
        ", ".join(missing),
    )

    for role in missing:
        subtask_id = f"{last_id}.{next_seq}"
        existing_subtasks.append({
            "id": subtask_id,
            "role": role,
            "description": (
                f"Analyze the task from the {role} perspective. "
                f"Identify risks, opportunities, constraints, and concrete "
                f"contributions this role can offer to the main deliverable."
            ),
            "artifact_name": f"{role}-lens-analysis",
            "risk": "LOW",
            "complexity": "standard",
            "dependencies": [],
        })
        next_seq += 1


PROJECT_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}[a-z0-9]$")


def _load_project_slugs() -> set[str]:
    """Return the set of active project slugs registered in the projects table.

    Empty set on any failure — validator degrades to "any slug accepted" rather
    than blocking the orchestrator on a DB hiccup. The planner is still forced
    to commit to *some* value, which is the main goal.
    """
    try:
        from okuro.db import get_db
        db = get_db()
        rows = db.fetchall("SELECT id FROM projects WHERE active = 1")
        return {r["id"] for r in rows if r.get("id")}
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("[project_slug] could not load project list: %s", exc)
        return set()


def _classify_project_slug(value, scope) -> tuple[str, str | None]:
    """Classify a plan's project_slug+scope pair.

    Returns (kind, normalized_slug):
        ("existing",  "<slug>")  — must be in active project list
        ("new",       "<slug>")  — proposed slug, validate naming convention
        ("system",    None)      — system-scoped task (cross-project)
        ("invalid",   None)      — could not parse
    """
    if value is None:
        if (scope or "").strip().lower() == "system":
            return ("system", None)
        return ("invalid", None)

    raw = str(value).strip()
    if raw.startswith("NEW:"):
        candidate = raw[4:].strip()
        if PROJECT_SLUG_PATTERN.match(candidate):
            return ("new", candidate)
        return ("invalid", None)

    if PROJECT_SLUG_PATTERN.match(raw):
        return ("existing", raw)

    return ("invalid", None)


def _suggest_similar_slugs(proposed: str, available: set[str], limit: int = 5) -> list[str]:
    """Return up to ``limit`` existing slugs that look similar to ``proposed``.

    Cheap substring + token-overlap heuristic — no fuzzy dep needed. Used in
    rejection feedback so the planner has concrete candidates to consider on
    retry.
    """
    if not available:
        return []
    proposed_lower = proposed.lower()
    proposed_tokens = set(re.split(r"[-_\s]+", proposed_lower)) - {""}

    scored: list[tuple[float, str]] = []
    for slug in available:
        score = 0.0
        slug_lower = slug.lower()
        if proposed_lower in slug_lower or slug_lower in proposed_lower:
            score += 2.0
        slug_tokens = set(re.split(r"[-_]+", slug_lower)) - {""}
        overlap = proposed_tokens & slug_tokens
        score += len(overlap)
        if score > 0:
            scored.append((score, slug))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [s for _, s in scored[:limit]]


def _assert_project_resolvable(plan_dict: dict, available_slugs: set[str]) -> tuple[str, str | None]:
    """Tri-state validator for the plan's project_slug field.

    Accepted:
      - existing slug (must be in active projects table)
      - "NEW:<slug>" prefix for greenfield work (slug must match naming convention,
         must NOT clash with an existing slug)
      - null + scope=system for cross-project/infra tasks

    Raises ``ValueError`` with planner-readable feedback on rejection. The
    error message is structured so callers can feed it back into the LLM for
    a deterministic retry.

    Returns (kind, slug) on success — useful for downstream tagging.
    """
    raw_slug = plan_dict.get("project_slug")
    scope = plan_dict.get("scope") or "project"

    kind, slug = _classify_project_slug(raw_slug, scope)

    if kind == "invalid":
        raise ValueError(
            "PROJECT_SLUG_INVALID: top-level `project_slug` is missing or malformed. "
            "Allowed values: an existing slug, `NEW:<proposed-slug>` for greenfield, "
            "or `null` with `scope: system` for cross-project work. "
            "Phase 0 Orient is mandatory — call list_projects() and cortex_search() "
            "before emitting the plan.\n"
            f"Got: project_slug={raw_slug!r}, scope={scope!r}"
        )

    if kind == "existing":
        if available_slugs and slug not in available_slugs:
            similar = _suggest_similar_slugs(slug, available_slugs)
            hint = f" Candidates that look similar: {', '.join(similar)}." if similar else ""
            raise ValueError(
                f"PROJECT_SLUG_UNKNOWN: `{slug}` is not a registered project. "
                f"Either pick an existing slug or declare greenfield with "
                f"`project_slug: NEW:{slug}`.{hint}"
            )
        return ("existing", slug)

    if kind == "new":
        if slug in available_slugs:
            raise ValueError(
                f"PROJECT_SLUG_CLASH: `NEW:{slug}` collides with an existing "
                f"registered project. If you meant to continue work on it, drop "
                f"the NEW: prefix. If you genuinely need a new project, choose a "
                f"different slug."
            )
        similar = _suggest_similar_slugs(slug, available_slugs)
        if similar:
            logger.warning(
                "[project_slug] greenfield NEW:%s — high-similarity existing slugs: %s",
                slug, ", ".join(similar),
            )
        return ("new", slug)

    # kind == "system"
    return ("system", None)


def call_llm_api(prompt: str, config: Config) -> str:
    """Call the orchestrator LLM for plain-text generation."""
    return call_llm(prompt, config)


PROMPT_ARGV_LIMIT = 96 * 1024
"""Threshold above which the prompt is piped via stdin instead of argv.

Linux ``ARG_MAX`` is typically reported as 2 MiB by ``getconf``, but the
*practical* limit subtracts the environment block and the argv pointer
table; in real shells it lands around 128 KiB. Continuation prompts that
concatenate the original task description, the in-flight plan, and a long
new instruction overflow that limit and ``execve(2)`` returns ``E2BIG``
(Errno 7) before the CLI even starts. Failure mode observed on
task-20260416-230116, 2026-05-01: ``LLM call failed: [Errno 7] Argument
list too long: 'claude'``. 96 KiB keeps healthy headroom for env vars on
all supported CLIs (claude/codex/gemini), each of which reads the prompt
from stdin when no positional argv prompt is provided.
"""


def call_llm(
    prompt: str,
    config: Config,
    intelligence: str = "",
    task_id: str | None = None,
) -> str:
    """Execute LLM call via CLI subprocess with retry logic.

    When ``intelligence == "max"``, the strategic-tier model for the decompose
    CLI is used instead of ``orchestrator.decompose_model``.

    When ``task_id`` is provided, the LLM call routes through
    ``bridge.invoke`` with ``activity_sink`` pointed at the task's
    ``.activity.jsonl`` so tokens stream into the FE activity feed
    during the 3–5 min decompose call (matches Critic + Scorer post-C10
    behaviour). Synthetic ``subtask_id="decomposer:<task_id>"`` +
    ``role="decomposer"`` tag every row so the FE groups them under a
    dedicated card. Without ``task_id`` the legacy blocking
    subprocess.run path runs unchanged (tests, sequential CLI utilities,
    and any caller that has no task to attribute activity to).
    """
    import time as _time

    cli_name = config.orchestrator.decompose_cli
    tool = get_cli_tool_config(cli_name, config, "orchestrator.decompose_cli")
    if intelligence == "max":
        model = tool.tier_map.get("strategic", config.orchestrator.decompose_model)
    else:
        model = config.orchestrator.decompose_model

    # Streaming path — only when we have a task_id AND a tasks_dir to write
    # the activity feed to. The streaming path mirrors Critic + Scorer
    # (reviewer/critic.py:305-330): bridge.invoke routes to
    # execute_streaming under the hood when activity_sink is set + provider
    # is claude. Non-claude providers fall back to non-streaming execute()
    # automatically — the activity sink is silently dropped.
    if task_id:
        activity_sink: Path | None = None
        try:
            activity_sink = config.tasks_dir / task_id / ".activity.jsonl"
            # Ensure the task dir exists; .activity.jsonl is created on
            # first append by the streaming executor (open mode "a").
            activity_sink.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            activity_sink = None

        if activity_sink is not None:
            try:
                from okuro.bridge.invoke import invoke as bridge_invoke
            except Exception as exc:
                logger.warning(
                    f"[DECOMPOSE] bridge import failed ({exc}); falling back to subprocess path"
                )
            else:
                max_attempts = 3
                backoff_delays = [5, 15]
                last_error: Exception | None = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        res = bridge_invoke(
                            prompt=prompt,
                            capability=None,
                            provider=cli_name,
                            model=model or None,
                            timeout=600,
                            activity_sink=activity_sink,
                            subtask_id=f"decomposer:{task_id}",
                            role="decomposer",
                        )
                    except Exception as e:
                        last_error = RuntimeError(f"LLM call failed: {e}")
                    else:
                        if res.get("success"):
                            return strip_bootstrap_greeting(res.get("output") or "")
                        last_error = RuntimeError(
                            f"LLM call failed: {res.get('error') or 'unknown error'}"
                        )
                    if attempt < max_attempts:
                        delay = backoff_delays[attempt - 1]
                        logger.warning(
                            f"LLM call attempt {attempt}/{max_attempts} failed, retrying in {delay}s..."
                        )
                        _time.sleep(delay)
                    else:
                        raise last_error

    flags = [f for f in tool.autonomous_flags if f not in ("--output-format", "stream-json", "--verbose")]
    # Skip the -m/--model arg when the tier_map value is empty — that means
    # "let the CLI pick its account-appropriate default". Pinning specific
    # model names (`o3`, `gemini-2.0-flash`, …) in the canon tier_map drifted:
    # Codex with a ChatGPT account rejects API-only models with a 400, Gemini
    # CLI returns 404 for deprecated names. Mac install report 2026-04-28.
    cmd = [tool.binary] + flags
    if tool.model_flag and model:
        cmd.extend([tool.model_flag, model])

    use_stdin = len(prompt) > PROMPT_ARGV_LIMIT
    if not use_stdin:
        cmd.append(prompt)

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    max_attempts = 3
    backoff_delays = [5, 15]
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(
                cmd,
                input=prompt if use_stdin else None,
                capture_output=True,
                text=True,
                timeout=600,
                env=env,
            )
            if result.returncode != 0:
                last_error = RuntimeError(
                    f"LLM call failed with exit code {result.returncode}\n"
                    f"STDERR: {result.stderr}\nSTDOUT: {result.stdout}"
                )
            else:
                # Single choke-point hygiene — strip the CLI's bootstrap
                # greeting so every caller (decomposer, suggestions,
                # capability harvester, …) sees only the model's answer.
                return strip_bootstrap_greeting(result.stdout)
        except FileNotFoundError:
            raise RuntimeError(f"CLI binary '{tool.binary}' not found")
        except subprocess.TimeoutExpired:
            last_error = RuntimeError("LLM call timed out after 600 seconds")
        except Exception as e:
            last_error = RuntimeError(f"LLM call failed: {e}")

        if attempt < max_attempts:
            delay = backoff_delays[attempt - 1]
            logger.warning(f"LLM call attempt {attempt}/{max_attempts} failed, retrying in {delay}s...")
            _time.sleep(delay)
        else:
            raise last_error


def parse_plan(response: str) -> dict:
    """Extract and parse YAML from LLM response."""
    yaml_block_pattern = r"```ya?ml\s*\n(.*?)\n```"
    match = re.search(yaml_block_pattern, response, re.DOTALL | re.IGNORECASE)
    if match:
        try:
            return yload(match.group(1))
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in code block: {e}")

    try:
        return yload(response)
    except yaml.YAMLError:
        pass

    for i, line in enumerate(response.split("\n")):
        if line.strip().startswith("phases:"):
            yaml_text = "\n".join(response.split("\n")[i:])
            try:
                return yload(yaml_text)
            except yaml.YAMLError as e:
                raise ValueError(f"Invalid YAML starting from 'phases:': {e}")

    raise ValueError("No valid YAML found in LLM response")


# Gate 2 §C11 / G1 — runtime-check AC detection.
#
# Patterns detect the SHAPE of a runtime check, not specific commands:
#   - "X exits 0" / "X returns 0" / "X exits with 0"
#   - "X succeeds" / "succeeds without error"
#   - "X prints Y" / "outputs Y" / "emits Y"
#   - "rtl ≤ 7ms" / "latency < 10ms" (numeric thresholds)
#   - "zero xruns" / "no errors"
#   - Shell-like fragments ("pytest -q", "which X", "dpkg -l")
# DP10 — pattern over instance. A new runtime check that uses words from
# the same family is picked up without code changes.
_RUNTIME_CHECK_PATTERNS = (
    re.compile(r"\b(?:exits?|returns?|exit-?code)\s+\d", re.IGNORECASE),
    re.compile(r"\b(?:succeed|succeeds|successful)\b", re.IGNORECASE),
    re.compile(r"\b(?:print|prints|emit|emits|output|outputs)\b", re.IGNORECASE),
    re.compile(r"\b(?:zero|no)\s+(?:xrun|error|warning|failure)s?\b", re.IGNORECASE),
    re.compile(r"[<>≤≥]=?\s*\d+\s*(?:ms|s|%|MB|GB)", re.IGNORECASE),
    re.compile(r"\$\s+\S+|\bpytest\b|\bdpkg\b|\bwhich\b|\bsystemctl\b|\bcurl\b",
               re.IGNORECASE),
    re.compile(r"\bcommand\s+(?:returns|exits|prints|succeeds)\b", re.IGNORECASE),
)


def _ac_is_runtime_check(ac_text: str) -> bool:
    """True iff the AC text looks like a runtime check.

    DP10: pattern-based; any AC whose prose names a command, exit code,
    success signal, output, or numeric threshold is treated as a runtime
    check. No hardcoded command list.
    """
    if not ac_text or not isinstance(ac_text, str):
        return False
    return any(p.search(ac_text) for p in _RUNTIME_CHECK_PATTERNS)


def _ac_slug(ac_text: str, idx: int) -> str:
    """Compact slug derived from the AC text. Falls back to ac<index+1>."""
    slug = re.sub(r"[^a-z0-9]+", "-", (ac_text or "").lower()).strip("-")
    slug = slug[:40].strip("-")
    return slug or f"ac{idx + 1}"


def _auto_derive_evidence_outputs(subtask_id: str, acs: list[str]) -> list[str]:
    """Synthesize evidence-log outputs for every runtime-check AC.

    Returns a list of `evidence/<subtask>-ac<i>-<slug>.log` paths — one
    per AC matching a runtime-check pattern. Namespaced under `evidence/`
    so they don't collide with the agent's user-facing artifact.

    Empty result for non-runtime ACs (prose deliverables, design choices,
    …) — those don't need captured stdout to verify.
    """
    if not acs:
        return []
    out: list[str] = []
    for i, ac in enumerate(acs):
        if not _ac_is_runtime_check(ac):
            continue
        slug = _ac_slug(ac, i)
        out.append(f"evidence/{subtask_id}-ac{i + 1}-{slug}.log")
    return out


# ROCK-SOLID v5 P5.7 — which subtasks are expected to leave a file behind.
#
# Role-based rather than description-keyword based: a role is a declared,
# closed vocabulary the plan already validates against, while description text
# is free-form and a keyword rule would fire on "research the build system".
# Deliberately narrow — a false positive nags about work that was never going
# to produce a file, and a warning that cries wolf is a warning nobody reads.
# ROCK-SOLID v5 P5.7 — severity, because `validate_plan` never had any.
#
# Every warning it returned before this was STRUCTURAL: a missing field, an
# unknown role, a dangling dependency, a duplicate id. "Any warning means the
# plan is broken" was therefore true, and `flow_compiler` encoded it by
# raising FlowCompileError on a non-empty list.
#
# The outputs warning below is the first ADVISORY one — a plan that trips it
# is well-formed and will run. Adding it without a severity turned every
# drawn workflow with a writer node into a compile error (18 tests, found the
# honest way: by running them). The prefix is the smallest thing that lets a
# caller gate on structure while still logging advice, and it reads correctly
# in a log line, which a severity enum would not.
ADVISORY_PREFIX = "advisory: "

_BUILD_SHAPED_ROLE_HINTS = (
    "engineer", "developer", "frontend", "backend", "fullstack",
    "designer", "writer", "author", "builder", "implementer",
)


def _is_build_shaped(subtask: dict) -> bool:
    """True when this subtask's role implies a file-producing deliverable."""
    role = str(subtask.get("role") or "").lower()
    if not role:
        return False
    return any(h in role for h in _BUILD_SHAPED_ROLE_HINTS)


def _write_intent_violations(plan: dict) -> list[str]:
    """Subtasks that say they will change files and declare nothing.

    P5.7 made this an advisory and it was ignored three times in one day —
    the gate can only be as honest as the declaration, and nothing checked
    the declaration against the subtask's own description.
    """
    out: list[str] = []
    for phase in plan.get("phases") or []:
        for st in phase.get("subtasks") or []:
            if not isinstance(st, dict):
                continue
            if _undeclared_write_intent(st):
                out.append(
                    f"subtask {st.get('id', '?')} ('{str(st.get('description') or '')[:90]}') "
                    "describes changing files but declares no `outputs` and no "
                    "`target_paths`"
                )
    return out


def _assert_write_intent_declared(plan: dict) -> None:
    """Reject a plan whose write-intent subtasks declare no paths.

    Raises ``ValueError`` so ``_plan_with_project_retry`` re-prompts ONCE with
    the violation named, then refuses. A refusal here is cheap; the failure it
    replaces is a file-mutating subtask that ran with no human gate and no LLM
    review, discovered afterwards by diffing md5 sums.
    """
    violations = _write_intent_violations(plan)
    if not violations:
        return
    raise ValueError(
        "WRITE_INTENT_UNDECLARED: " + "; ".join(violations)
        + ". Every subtask that edits, creates, renames or deletes anything "
        "on disk MUST list the paths it will touch in `target_paths` (and its "
        "deliverable in `outputs`). Declare them, or re-scope the subtask so "
        "it does not write."
    )


def validate_plan(plan: dict, role_index: dict,
                   registered_capabilities: set[str] | None = None) -> list[str]:
    """Validate the decomposed plan. Returns warning list.

    Wave-3 G6: when ``registered_capabilities`` is provided (set of capability
    ids known to ``CapabilityRegistry``), enforce that every
    ``reuses_capabilities`` entry has either an in-plan producer
    (``produces_capability``) or a registry hit. In-plan producers also get
    auto-promoted to a hard dependency when the consumer forgot to declare
    it — silent dependency holes were the dominant cause of plans where
    subtask N re-implements what subtask M already produced.
    """
    warnings = []

    if "phases" not in plan:
        warnings.append("Missing 'phases' key in plan")
        return warnings

    all_subtask_ids = set()

    for phase in plan["phases"]:
        if "subtasks" not in phase:
            warnings.append(f"Phase {phase.get('id', '?')} missing 'subtasks'")
            continue

        for subtask in phase["subtasks"]:
            required_fields = ["id", "role", "description", "risk", "complexity"]
            for field_name in required_fields:
                if field_name not in subtask:
                    warnings.append(f"Subtask {subtask.get('id', '?')} missing: {field_name}")

            if "id" in subtask:
                st_id = subtask["id"]
                if st_id in all_subtask_ids:
                    warnings.append(f"Duplicate subtask ID: '{st_id}'")
                all_subtask_ids.add(st_id)

            # ROCK-SOLID v5 P5.7 — mechanism over discipline.
            #
            # `outputs` / `target_paths` are rank 0 of preview resolution: the
            # in-code declaration of what this subtask sets out to produce, and
            # the only signal that beats every heuristic. Both of the plan's
            # existing mitigations for a missed preview (declare your outputs;
            # set project_path in the handover) are SUBAGENT DISCIPLINE — they
            # work exactly as often as an agent remembers them, which over a
            # long run means "sometimes".
            #
            # Requiring it at PLAN time makes it a property of the plan
            # instead, checked once by code that cannot forget. A warning, not
            # a rejection: some build-shaped work legitimately produces only a
            # brain artifact, and P5.1 previews that correctly now, so
            # refusing the plan would trade one false negative for a harder
            # failure.
            if _is_build_shaped(subtask) and not (
                subtask.get("outputs") or subtask.get("target_paths")
            ):
                warnings.append(
                    f"{ADVISORY_PREFIX}"
                    f"Subtask {subtask.get('id', '?')}: build-shaped work "
                    f"(role '{subtask.get('role', '?')}') declares neither "
                    "outputs nor target_paths — its deliverable will be found "
                    "by heuristic scan, or not at all"
                )

            # THE RISK FLOOR. A description that says it will change files is
            # not LOW risk, whatever the planner scored it.
            #
            # Measured 2026-08-01: risk LOW is the single score that removes
            # BOTH safety mechanisms at once — the blast-radius approval gate
            # is only consulted at MED/HIGH, and fast-track requires every
            # subtask in the phase to be LOW before it skips the LLM critic. A
            # subtask that wrote 8 files outside any workspace passed through
            # both, scored LOW by the same planner that wrote the file
            # mutation into the description.
            #
            # Applied here rather than at either enforcement point because
            # both read the score, and a floor at the source fixes them
            # together. Only ever raises the score.
            if _has_write_intent(subtask.get("description")) and (
                subtask.get("risk") in (None, "LOW")
            ):
                warnings.append(
                    f"Subtask {subtask.get('id', '?')}: risk raised "
                    f"{subtask.get('risk') or 'unset'} → MED — its description "
                    "says it will change files, which is not LOW-risk work"
                )
                subtask["risk"] = "MED"

            if "role" in subtask and subtask["role"] not in role_index:
                warnings.append(f"Subtask {subtask.get('id', '?')}: role '{subtask['role']}' not in index")

            if subtask.get("risk") not in (None, "LOW", "MED", "HIGH"):
                warnings.append(f"Subtask {subtask.get('id', '?')}: invalid risk '{subtask['risk']}'")

            if subtask.get("complexity") not in (None, "fast", "standard", "strategic"):
                warnings.append(f"Subtask {subtask.get('id', '?')}: invalid complexity '{subtask['complexity']}'")

            qa_roles = {"qa-engineer", "reviewer", "security-auditor"}
            if subtask.get("role") in qa_roles and subtask.get("complexity") == "fast":
                warnings.append(f"Subtask {subtask.get('id', '?')}: '{subtask['role']}' upgraded from fast to standard")
                subtask["complexity"] = "standard"

            # P0-4 — artifact_name / outputs naming drift normalization.
            # The decomposer LLM frequently emits two different stems for
            # the same subtask (e.g. artifact_name='audio-stack-survey'
            # but outputs=['audio-stack-comparison.md']). The dispatcher's
            # subagent prompt uses artifact_name; the reviewer uses
            # outputs[0]. Mismatch → subagent writes a file the reviewer
            # will never find. Normalize: artifact_name is load-bearing
            # (flows into the actual write path), so coerce outputs[0]
            # to match it whenever they disagree. Adds the original
            # outputs[0] stem to outputs[1+] as a hint for the reviewer
            # so the legacy expectation is preserved as a non-blocking
            # alias.
            _aname = (subtask.get("artifact_name") or "").strip()
            _outs = subtask.get("outputs") or []
            if _aname and _outs and isinstance(_outs[0], str):
                _expected = f"{_aname}.md" if not _aname.endswith(".md") else _aname
                if _outs[0] != _expected:
                    warnings.append(
                        f"Subtask {subtask.get('id', '?')}: artifact_name="
                        f"{_aname!r} disagrees with outputs[0]={_outs[0]!r}; "
                        f"normalizing outputs[0] to {_expected!r}."
                    )
                    subtask["outputs"] = [_expected] + [o for o in _outs if o != _expected]

            # Gate 2 §C11 / G1 — auto-derive evidence outputs for
            # runtime-check acceptance criteria. When an AC matches a
            # runtime-check shape (commands, exit-codes, succeeds, prints,
            # returns), synthesize an `evidence/<slug>.log` deliverable in
            # outputs[]. The dispatcher's evidentiary block + the
            # deterministic file_exists check both pick it up — the
            # subagent no longer has to guess that the reviewer expects
            # captured stdout. DP10: pattern-based, not specific.
            _acs = subtask.get("acceptance_criteria") or []
            if isinstance(_acs, list) and _acs:
                synthetic = _auto_derive_evidence_outputs(
                    subtask.get("id", "?"), [str(a) for a in _acs],
                )
                if synthetic:
                    existing = set(subtask.get("outputs") or [])
                    added = [s for s in synthetic if s not in existing]
                    if added:
                        subtask["outputs"] = list(subtask.get("outputs") or []) + added
                        warnings.append(
                            f"Subtask {subtask.get('id', '?')}: auto-derived "
                            f"{len(added)} evidence output(s) from runtime-check "
                            f"acceptance criteria: {added}"
                        )

    # Dependency + cycle validation
    dep_graph = {}
    for phase in plan["phases"]:
        for subtask in phase.get("subtasks", []):
            st_id = subtask.get("id")
            deps = subtask.get("dependencies", [])
            if st_id:
                dep_graph[st_id] = [str(d) for d in deps]
            for dep_id in deps:
                if dep_id not in all_subtask_ids:
                    warnings.append(f"Subtask {subtask.get('id', '?')}: dependency '{dep_id}' not found")

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in dep_graph}

    def _dfs_cycle(node, path):
        color[node] = GRAY
        path.append(node)
        for dep in dep_graph.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                cycle_start = path.index(dep)
                return path[cycle_start:] + [dep]
            if color[dep] == WHITE:
                result = _dfs_cycle(dep, path)
                if result:
                    return result
        path.pop()
        color[node] = BLACK
        return None

    for node in dep_graph:
        if color[node] == WHITE:
            cycle = _dfs_cycle(node, [])
            if cycle:
                warnings.append(f"Circular dependency: {' -> '.join(str(c) for c in cycle)}")
                break

    # Wave-3 G6 — capability dep enforcement. Build (capability_id → producer
    # subtask_id) from produces_capability fields, then walk every consumer
    # (reuses_capabilities) and either:
    #   - auto-add the missing dep edge to the in-plan producer + warn, OR
    #   - warn that the capability has no in-plan producer AND isn't in the
    #     registered set (when supplied) — meaning the consumer will have
    #     no upstream context and will re-implement from scratch.
    producer_of: dict[str, str] = {}
    for phase in plan["phases"]:
        for st in phase.get("subtasks", []):
            cap = st.get("produces_capability")
            if cap and isinstance(cap, str):
                cap = cap.strip()
                if cap and cap not in producer_of:
                    producer_of[cap] = str(st.get("id", ""))

    for phase in plan["phases"]:
        for st in phase.get("subtasks", []):
            consumed = st.get("reuses_capabilities") or []
            if not isinstance(consumed, list):
                continue
            st_id = str(st.get("id", "?"))
            deps = st.get("dependencies") or []
            # Coerce to a mutable list of strings so we can auto-add edges.
            deps_list = [str(d) for d in deps]
            for cap_id in consumed:
                if not isinstance(cap_id, str) or not cap_id.strip():
                    continue
                cap_id = cap_id.strip()
                producer = producer_of.get(cap_id)
                if producer:
                    # In-plan producer exists. Verify dep edge — auto-add if missing.
                    if producer != st_id and producer not in deps_list:
                        deps_list.append(producer)
                        warnings.append(
                            f"Subtask {st_id}: reuses '{cap_id}' produced by "
                            f"{producer} but missing dependency edge — auto-added."
                        )
                else:
                    # No in-plan producer. Check registry membership.
                    if registered_capabilities is None:
                        warnings.append(
                            f"Subtask {st_id}: reuses '{cap_id}' but no in-plan "
                            "producer. Capability registry not consulted "
                            "(registered_capabilities=None)."
                        )
                    elif cap_id not in registered_capabilities:
                        warnings.append(
                            f"Subtask {st_id}: reuses '{cap_id}' but no in-plan "
                            "producer AND not in capability registry — consumer "
                            "will re-implement from scratch."
                        )
            # Persist any auto-added deps back onto the subtask dict so
            # plan_to_phases sees them.
            if deps_list != [str(d) for d in deps]:
                st["dependencies"] = deps_list

    # P1-3 — role-prerequisite enforcement. Some review/audit roles must
    # run AFTER another review role, never in parallel (audio task: license-
    # auditor PASSed while qa-engineer FAILed). Without this pass the
    # planner often lists them as siblings under the same phase. We walk
    # _ROLE_PREREQUISITES and auto-add the missing dep edge so the
    # orchestrator blocks the dependent until the prerequisite finishes.
    _enforce_role_prerequisites(plan, warnings)

    return warnings


# Role precedence — dependent role → set of prerequisite roles. A subtask
# whose role is in the dict gets a dependency added to every subtask whose
# role matches one of the prerequisites IF such a prerequisite exists in
# the plan AND the edge is not already declared. The dict is intentionally
# narrow; widening it without a documented incident regresses parallelism
# for the rest of the plan.
_ROLE_PREREQUISITES: dict[str, set[str]] = {
    "license-auditor": {"qa-engineer"},
}


def _enforce_role_prerequisites(plan: dict, warnings: list[str]) -> None:
    """Auto-add missing dependency edges between role-prerequisite pairs.

    Mutates plan in-place. Records each addition in ``warnings``.
    Safe to call even when no roles match — no-op on plans without the
    listed dependents.
    """
    # First pass — index every subtask id by its role so we can find the
    # prerequisite producers across phases.
    by_role: dict[str, list[str]] = {}
    for phase in plan.get("phases", []) or []:
        for st in phase.get("subtasks", []) or []:
            role = st.get("role")
            stid = st.get("id")
            if not role or not stid:
                continue
            by_role.setdefault(role, []).append(str(stid))

    # Second pass — for every dependent subtask, ensure its prerequisite
    # roles are upstream of it. Skip self-loops so a dependent role listed
    # as its own prerequisite (configuration error) is silently ignored.
    for phase in plan.get("phases", []) or []:
        for st in phase.get("subtasks", []) or []:
            role = st.get("role")
            stid = str(st.get("id") or "")
            prereqs = _ROLE_PREREQUISITES.get(role or "")
            if not prereqs or not stid:
                continue
            deps_list = [str(d) for d in (st.get("dependencies") or [])]
            added: list[str] = []
            for prereq_role in prereqs:
                for prereq_id in by_role.get(prereq_role, []):
                    if prereq_id == stid:
                        continue
                    if prereq_id in deps_list:
                        continue
                    deps_list.append(prereq_id)
                    added.append(prereq_id)
            if added:
                st["dependencies"] = deps_list
                warnings.append(
                    f"Subtask {stid} ({role}): added prerequisite role edge "
                    f"→ {', '.join(added)}"
                )


def plan_to_phases(plan_dict: dict) -> list[Phase]:
    """Convert parsed YAML dict to list of Phase objects."""
    if not plan_dict or "phases" not in plan_dict:
        raise ValueError("Plan dict missing 'phases' key")

    phases = []
    for i, phase_data in enumerate(plan_dict["phases"]):
        if not isinstance(phase_data, dict):
            raise ValueError(f"Phase {i} is not a dict")

        phase_id = phase_data.get("id")
        phase_name = phase_data.get("name")
        if phase_id is None or phase_name is None:
            raise ValueError(f"Phase {i} missing 'id' or 'name'")

        subtasks = []
        for j, st_data in enumerate(phase_data.get("subtasks", [])):
            if not isinstance(st_data, dict):
                raise ValueError(f"Subtask {j} in phase {phase_id} is not a dict")

            required = ["id", "role", "description", "risk", "complexity"]
            missing = [f for f in required if f not in st_data]
            if missing:
                raise ValueError(f"Subtask {st_data.get('id', j)} in phase {phase_id} missing: {missing}")

            subtasks.append(Subtask(
                id=str(st_data["id"]),
                role=st_data["role"],
                description=st_data["description"],
                risk=st_data["risk"],
                complexity=st_data["complexity"],
                status="pending",
                dependencies=st_data.get("dependencies", []),
                artifact_name=st_data.get("artifact_name", ""),
                phase=phase_id,
                # Provenance from a DRAWN workflow, when there was one. The flow
                # compiler writes `_node_id`; every other planning path omits it
                # and the field stays empty.
                node_id=str(st_data.get("_node_id") or ""),
                produces_capability=st_data.get("produces_capability") or None,
                reuses_capabilities=st_data.get("reuses_capabilities") or [],
                # Wave-4 G8 — typed contract metadata (all optional).
                inputs=[str(x) for x in (st_data.get("inputs") or [])],
                outputs=[str(x) for x in (st_data.get("outputs") or [])],
                acceptance_criteria=[str(x) for x in (st_data.get("acceptance_criteria") or [])],
                target_paths=[str(x) for x in (st_data.get("target_paths") or [])],
                contract_id=str(st_data.get("contract_id") or ""),
            ))

        raw_supersedes = phase_data.get("supersedes") or []
        supersedes = [str(x) for x in raw_supersedes if x]
        serialize = bool(phase_data.get("serialize", False))

        # M1.5 — accept an inline `decision_gate` block on the phase. The
        # decomposer emits these when intelligence=max sees a load-bearing
        # architectural choice (stack pick, transport, library). Missing
        # field = no gate (legacy planner output stays valid).
        decision_gate = None
        gate_dict = phase_data.get("decision_gate")
        if isinstance(gate_dict, dict):
            options_raw = gate_dict.get("options") or []
            opts: list[DecisionGateOption] = []
            for j, o in enumerate(options_raw):
                if not isinstance(o, dict):
                    continue
                try:
                    opts.append(DecisionGateOption(
                        id=str(o.get("id") or f"opt-{j+1}"),
                        label=str(o.get("label") or ""),
                        description=str(o.get("description") or ""),
                        pros=str(o.get("pros") or ""),
                        cons=str(o.get("cons") or ""),
                        risk=str(o.get("risk") or ""),
                        recommended=bool(o.get("recommended") or False),
                    ))
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        f"phase {phase_id}: skipping malformed gate option {j}: {exc}"
                    )
            if opts:
                decision_gate = DecisionGate(
                    id=str(gate_dict.get("id") or f"gate-phase-{phase_id}"),
                    prompt=str(gate_dict.get("prompt") or "Pick an option"),
                    options=opts,
                )
                # A gate-bearing phase serialises by default — a parallel
                # dispatch on a phase the user just made an ADR for would
                # defeat the purpose of asking. Planner can still set
                # serialize:false explicitly to override (rare).
                if "serialize" not in phase_data:
                    serialize = True
            else:
                logger.warning(
                    f"phase {phase_id}: decision_gate present but no valid options — dropped"
                )

        phases.append(Phase(
            id=phase_id,
            name=phase_name,
            subtasks=subtasks,
            status="pending",
            supersedes=supersedes,
            serialize=serialize,
            decision_gate=decision_gate,
        ))

    return phases


def decompose_continuation(continuation_instructions: str, task, config: Config) -> list[Phase]:
    """Decompose continuation instructions in context of an existing task.

    Wave-2 G2: render Stream A handover briefs (decisions, open_questions,
    cortex_refs) per completed subtask instead of ``output_summary[:300]``.
    The 300-char summary was the dispatcher's lines[:40] killer reincarnated
    at the planner layer — continuation re-planning was blind to upstream
    decisions, leaving the new plan to re-derive everything from prose.
    """
    # PR A — continuation context-audit transparency. Pre-fix this function
    # ran 5–20s silently, FE rendered nothing. The decomposer-activity-panel
    # filters on subtask_id startswith("decomposer:") OR ("continuation:"),
    # so emit a milestone per context source consulted.
    task_id_for_emit = getattr(task, "id", None)

    def _cm(label: str, **fields: object) -> None:
        if not task_id_for_emit:
            return
        try:
            path = config.tasks_dir / task_id_for_emit / ".activity.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "type": "thinking",
                "subtask_id": f"continuation:{task_id_for_emit}",
                "role": "decomposer",
                "ts": _dt.utcnow().isoformat(),
                "preview": label,
            }
            row.update(fields)
            with open(path, "a") as fh:
                fh.write(_json.dumps(row) + "\n")
        except Exception:
            pass

    _cm("Reading context: prior phases, handovers, artifacts.", stage="context_audit_started")

    try:
        from okuro.sense.role_handover import read_role_handover as _read_handover
    except Exception:  # pragma: no cover — storage unavailable in some test envs
        _read_handover = None

    # ------------------------------------------------------------------
    # Bounded completed-work context (replaces the pre-fix all-subtasks
    # dump). The old loop walked EVERY phase × EVERY subtask, inlining
    # each handover (outcome + summary + decisions[:5] + open_questions[:5]
    # + cortex_refs[:5]) with no total cap. At ~880 chars/subtask the
    # prompt grew linearly: ~42KB @ 15 subtasks, ~135KB @ 120 — it
    # degraded the planner at 60+ subtasks / multi-continuation tasks.
    #
    # The bounded assembly mirrors the dispatcher brief path:
    #   1. the latest M2 compressor decision-trace (≤5K tok) as the
    #      single "prior work" summary, PLUS
    #   2. the TOP-K cortex-relevance-ranked subtask handovers (keyed on
    #      the continuation instructions) inlined with the SAME per-field
    #      caps, PLUS
    #   3. the rest as an id-list with a fetch sentinel.
    # If the compressor/embeddings are unavailable, fall back to the
    # most-recent-K handovers (still bounded — never the full dump).
    _HANDOVER_TOPK = 8  # inline at most K handovers; rest become an id-list

    def _render_handover(st_id: str, st_role: str, st_status: str,
                         st_desc: str, ho: dict | None,
                         output_summary: str | None) -> list[str]:
        """Render ONE subtask's bounded context block. Keeps the existing
        per-field caps (decisions[:5], open_questions[:5], cortex_refs[:5],
        legacy output_summary[:300])."""
        out = [f"- {st_id} ({st_role}, {st_status}): {st_desc}"]
        if ho:
            brief = ho.get("brief") or {}
            outcome = brief.get("outcome", "?")
            summary = (brief.get("summary") or "").strip()
            out.append(f"  Outcome: **{outcome}**")
            if summary:
                out.append(f"  Summary: {summary}")
            for d in (brief.get("decisions") or [])[:5]:
                if isinstance(d, dict) and d.get("decision"):
                    rat = d.get("rationale", "")
                    out.append(
                        f"  - decision: {d['decision']}"
                        + (f" ({rat})" if rat else "")
                    )
            for q in (brief.get("open_questions") or [])[:5]:
                if isinstance(q, str):
                    out.append(f"  - open_question: {q}")
            for r in (ho.get("cortex_refs") or [])[:5]:
                if isinstance(r, dict) and r.get("path"):
                    s = r.get("start_line", "?")
                    e = r.get("end_line", "?")
                    purp = r.get("purpose", "")
                    out.append(
                        f"  - ref: `{r['path']}` L{s}-{e}"
                        + (f" — {purp}" if purp else "")
                    )
        elif output_summary:
            # Crossover fallback: subtask completed pre-035 with no
            # handover row — keep the legacy summary so old tasks still
            # feed continuation planning.
            out.append(
                f"  Output: {output_summary[:300].replace(chr(10), ' ')}"
            )
        return out

    # Index every subtask (cheap; descriptions only) so we can render the
    # ranked top-K inline and list the remainder by id.
    all_subtasks: list = []  # (st, phase) in plan order
    st_by_id: dict = {}
    for phase in task.phases:
        for st in phase.subtasks:
            all_subtasks.append((st, phase))
            st_by_id[st.id] = (st, phase)

    completed_lines: list[str] = []

    # (1) M2 decision-trace — primary bounded "prior work" summary.
    _cm("Reading M2 decision-trace (compressor).", stage="context_trace")
    trace_md = ""
    try:
        from okuro.sense.task_events import latest_compression
        from okuro.sense.artifacts import artifact_get
        _comp = latest_compression(task_id=task.id)
        if _comp:
            _cb = _comp.get("body") or {}
            _aid = _cb.get("artifact_id") or ""
            if _aid:
                _art = artifact_get(_aid, include_body=True)
                if _art:
                    trace_md = (_art.get("body") or "").strip()
            if trace_md:
                _slo = _cb.get("covers_seq_from", "?")
                _shi = _cb.get("covers_seq_to", "?")
                completed_lines.append(
                    f"\n### Prior Work — Decision Trace (compressor, "
                    f"events seq {_slo}–{_shi})"
                )
                completed_lines.append(
                    "Distilled cross-subtask context. This is the bounded "
                    "summary of completed work — read it instead of "
                    "re-deriving from per-subtask history."
                )
                completed_lines.append(trace_md)
    except Exception:
        trace_md = ""

    # (2) Top-K cortex-relevance-ranked handovers keyed on the new
    # instructions. Falls back to most-recent-K when ranking is
    # unavailable — either way bounded, never the full dump.
    ranked_ids: list[str] = []
    ranking_mode = "relevance"
    try:
        from okuro.sense.role_handover import rank_role_handovers_by_relevance
        ranked = rank_role_handovers_by_relevance(
            task_id=task.id,
            query=continuation_instructions,
            limit=_HANDOVER_TOPK,
        )
        ranked_ids = [r.get("subtask_id") for r in ranked if r.get("subtask_id")]
    except Exception:
        ranked_ids = []
    if not ranked_ids:
        # Fallback: most-recent-K subtasks (plan order, tail), still bounded.
        ranking_mode = "recency"
        ranked_ids = [st.id for st, _ in all_subtasks][-_HANDOVER_TOPK:]
    # De-dupe while preserving rank order, and keep only known subtasks.
    seen: set = set()
    topk_ids: list[str] = []
    for sid in ranked_ids:
        if sid in st_by_id and sid not in seen:
            seen.add(sid)
            topk_ids.append(sid)

    _cm(
        f"Selected top-{len(topk_ids)} handover(s) by {ranking_mode}; "
        f"{max(0, len(all_subtasks) - len(topk_ids))} listed by id.",
        stage="context_topk",
    )

    if topk_ids:
        completed_lines.append(
            f"\n### Most-Relevant Completed Subtasks "
            f"(top {len(topk_ids)} by {ranking_mode})"
        )
        for sid in topk_ids:
            st, phase = st_by_id[sid]
            ho = None
            if _read_handover is not None:
                try:
                    ho = _read_handover(subtask_id=st.id, task_id=task.id)
                except Exception:
                    ho = None
            completed_lines.append(f"_(Phase {phase.id}: {phase.name})_")
            completed_lines.extend(
                _render_handover(
                    st.id, st.role, st.status, st.description,
                    ho, st.output_summary,
                )
            )

    # (3) The remainder as an id-list with a fetch sentinel (mirrors the
    # dispatcher dep-overflow idiom). Keeps the block bounded regardless
    # of subtask count.
    overflow = [
        (st, phase) for st, phase in all_subtasks if st.id not in seen
    ]
    if overflow:
        completed_lines.append(
            f"\n### Other Completed Subtasks "
            f"({len(overflow)} more — listed by id)"
        )
        completed_lines.append(
            "Not inlined to keep this plan bounded. Fetch any you need via "
            f"`list_role_handovers(task_id=\"{task.id}\", limit=200)` or "
            "`cortex_read_section(path, start, end)` for the referenced code."
        )
        for st, phase in overflow:
            completed_lines.append(
                f"- `{st.id}` ({st.role}, {st.status}): {st.description[:120]}"
            )

    completed_summary = "\n".join(completed_lines)

    next_phase_id = max(p.id for p in task.phases) + 1 if task.phases else 1

    prompt_path = config.prompts_dir / "decompose.md"
    with open(prompt_path, "r") as f:
        prompt_template = f.read()

    principles_text = ""
    principles_path = config.knowledge_dir / "global" / "principles.md"
    if principles_path.exists():
        with open(principles_path, "r") as f:
            principles_text = f.read()

    role_index = load_role_index()
    role_index_compact = format_role_index(role_index)

    capabilities_summary = ""
    try:
        from okuro.orchestrator.capabilities.registry import CapabilityRegistry
        cap_path = config.orchestrator_root / "capabilities.yaml"
        registry = CapabilityRegistry(cap_path)
        capabilities_summary = registry.format_for_prompt(max_caps=15)
    except Exception:
        pass

    # Build the set of subtasks the planner is allowed to supersede.
    # Pending/approved subtasks from prior phases are obsolete-able by
    # this correction; running/done/failed are not (done/failed are
    # historical truth; running must be cancelled first via the UI which
    # resets it to pending and makes it eligible here).
    from okuro.orchestrator.state import SUPERSEDEABLE_STATUSES
    supersedeable_subtasks: list[tuple[str, str, str]] = []  # (id, role, description)
    for ph in task.phases:
        for st in ph.subtasks:
            if st.status in SUPERSEDEABLE_STATUSES:
                supersedeable_subtasks.append((st.id, st.role, st.description[:120]))
    supersedeable_ids = {sid for sid, _, _ in supersedeable_subtasks}

    if supersedeable_subtasks:
        supersedeable_block = (
            "\n### Pending Subtasks That CAN Be Superseded\n"
            "If the user's correction makes any of these obsolete, mark them "
            "via the top-level `supersedes:` field on the new phase that "
            "replaces them. Do NOT silently leave them queued — they will run.\n\n"
            + "\n".join(
                f"- `{sid}` ({role}): {desc}"
                for sid, role, desc in supersedeable_subtasks
            )
            + "\n"
        )
    else:
        supersedeable_block = ""

    continuation_task = (
        f"## CONTINUATION OF EXISTING TASK\n\n"
        f"### Original Task\n{task.description}\n\n"
        f"### Completed Work\n{completed_summary}\n"
        f"{supersedeable_block}\n"
        f"### New Instructions (PLAN ONLY THIS)\n{continuation_instructions}\n\n"
        f"### IMPORTANT\n"
        f"- Phase numbering MUST start at {next_phase_id}\n"
        f"- Subtask IDs follow format \"{next_phase_id}.1\", \"{next_phase_id}.2\", etc.\n"
        f"- You can reference completed subtask IDs as dependencies if relevant\n"
        f"- Plan ONLY the new work — do NOT repeat completed phases\n"
        f"- For each new phase that obsoletes prior pending subtasks, emit a "
        f"top-level `supersedes: [<id>, ...]` field listing those subtask ids. "
        f"Only pending/approved subtasks from the prior plan are valid "
        f"supersedes targets (done/failed are historical; running must be "
        f"cancelled first).\n"
    )

    filled_prompt = prompt_template.replace("{principles_text}", principles_text)
    filled_prompt = filled_prompt.replace("{role_index_compact}", role_index_compact)
    filled_prompt = filled_prompt.replace("{capabilities_summary}", capabilities_summary)
    filled_prompt = filled_prompt.replace("{task_description}", continuation_task)

    available_slugs = _load_project_slugs()

    def _validate_supersedes(plan_dict: dict) -> None:
        new_ids: set[str] = set()
        for ph in plan_dict.get("phases") or []:
            for st in ph.get("subtasks") or []:
                if st.get("id"):
                    new_ids.add(str(st["id"]))
        _assert_supersedes_targets_valid(plan_dict, supersedeable_ids, new_ids)

    _cm(
        f"Context audit done — {len(task.phases)} prior phase(s), "
        f"{sum(len(p.subtasks) for p in task.phases)} subtask(s) inspected.",
        stage="context_audit_done",
        prior_phase_count=len(task.phases),
    )
    _cm("Invoking planner for continuation phases.", stage="llm_call")
    plan_dict = _plan_with_project_retry(
        filled_prompt, config, intelligence="",
        available_slugs=available_slugs,
        extra_validators=[_validate_supersedes, _assert_write_intent_declared],
        task_id=getattr(task, "id", None) or None,
    )

    warnings = validate_plan(
        plan_dict, role_index,
        registered_capabilities=_load_registered_capability_ids(config),
    )
    if warnings:
        for w in warnings:
            logger.warning(f"[CONTINUE] {w}")

    phases_out = plan_to_phases(plan_dict)
    _cm(
        f"Continuation plan built — {len(phases_out)} new phase(s) appended.",
        stage="done",
        new_phase_count=len(phases_out),
    )
    return phases_out


def format_role_index(role_index: dict) -> str:
    lines = []
    for role_name, role_info in sorted(role_index.items()):
        domain = role_info.get("domain", "N/A")
        tier = role_info.get("tier", "standard")
        description = role_info.get("description") or _get_role_description(role_name, role_info)
        line = f"- {role_name} ({domain}, {tier})"
        if description:
            line += f" - {description}"
        lines.append(line)
    return "\n".join(lines)


_role_description_cache: dict = {}


def _get_role_description(role_name: str, role_info: dict) -> str:
    if role_name in _role_description_cache:
        return _role_description_cache[role_name]
    desc = role_info.get("description", "")
    _role_description_cache[role_name] = desc
    return desc
