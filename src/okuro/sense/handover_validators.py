# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Role-handover payload validators V1-V11 — reject a malformed handover
#   before the storage layer or the KG ever sees it.
# index:
#   imports
#   def _has_fenced_code
#   def _produced_files_for
#   def _validate_role_handover
# AGENT_HEADER_END -->
"""Structural validation for `write_role_handover` payloads.

EXTRACTED FROM mcp_middleware.py, 2026-09-09. These ~400 lines are payload
schema enforcement for exactly one tool and have nothing to do with session
compliance, which is what the rest of that module does. Living together made
the middleware a 1582-line file with four unrelated jobs, and the gate logic
the file is named for was the smallest of them.

The contract is unchanged and still non-bypassable: `pre_tool_call` calls
:func:`_validate_role_handover` before dispatch, so a rejected handover is
never persisted and inserts no KG triples. `okuro.sense.role_handover` keeps a
thin defence-in-depth check, but the canonical contract is here.

Names keep their leading underscore and are re-exported from
`okuro.sense.mcp_middleware`, because that is where every existing caller and
test imports them from and this move is not the place to break that.
"""

import os

from okuro.sense.guard_log import guard_failed as _guard_failed

# ---------------------------------------------------------------------------
# Role-handover validators V1-V8
# ---------------------------------------------------------------------------
#
# Per artifact 65d87d98 §3 + HR1-HR9 (convention memory). These run in
# `pre_tool_call` for `write_role_handover` and reject malformed payloads
# BEFORE the storage layer ever sees them — no half-written rows, no
# orphan KG triples. The contract is non-bypassable: a producer must
# re-emit a clean handover, the .md (Stream B = artifact_write) is
# unaffected.

_FENCED_CODE_RE = "```"
_HANDOVER_SUMMARY_MAX = 600
_HANDOVER_DECISION_MAX = 200
_HANDOVER_RATIONALE_MAX = 400
_HANDOVER_QUESTION_MAX = 240
_HANDOVER_PURPOSE_MAX = 160
_HANDOVER_DECISIONS_HARDCAP = 25  # soft-warn at 10, hard-fail at 25 (C3)


def _has_fenced_code(text) -> bool:
    if not isinstance(text, str):
        return False
    return _FENCED_CODE_RE in text


def _produced_files_for(subtask_id: str) -> list[str]:
    """V3 — read orchestrator state for files the producer wrote.

    Returns the file paths under ``artifacts/`` that the producing subtask
    materialised. Walks ``state._index_task_artifacts`` watermark + a
    direct dir scan of ``tasks/{any}/artifacts/{subtask_id}*``. Best-effort
    — a missing tasks dir means we cannot enforce V3 and the validator
    soft-passes (rejecting on no signal would block agents in fresh
    sandboxes / unit tests).
    """
    try:
        from okuro.orchestrator.config import Config
        cfg = Config.load() if hasattr(Config, "load") else Config()
        tasks_dir = getattr(cfg, "tasks_dir", None)
    except Exception as exc:
        _guard_failed("orchestrator_config", exc)
        return []

    if tasks_dir is None:
        return []

    from pathlib import Path

    base = Path(tasks_dir)
    if not base.exists():
        return []

    produced: list[str] = []
    prefix = subtask_id + "-"
    bare = subtask_id + "."
    for task_dir in base.iterdir():
        artifacts = task_dir / "artifacts"
        if not artifacts.is_dir():
            continue
        for entry in artifacts.iterdir():
            name = entry.name
            if name.endswith(".stdout.md") or name.endswith(".stderr.md"):
                continue
            if name.startswith(prefix) or name.startswith(bare):
                produced.append(str(entry))
        # Also scan a per-subtask subdir if the agent created one.
        sub = artifacts / subtask_id
        if sub.is_dir():
            for entry in sub.rglob("*"):
                if entry.is_file():
                    produced.append(str(entry))
    return produced


def _validate_role_handover(arguments: dict) -> str | None:
    """Apply V1-V8 to ``write_role_handover`` arguments.

    Returns ``None`` when the payload is acceptable, otherwise a
    user-facing rejection message. The caller (pre_tool_call) returns
    ``allow=False`` with this message.
    """
    if not isinstance(arguments, dict):
        return "REJECTED: write_role_handover arguments must be a JSON object."

    subtask_id = (arguments.get("subtask_id") or "").strip()
    from_role = (arguments.get("from_role") or "").strip()
    brief = arguments.get("brief")

    # V6 — mandatory fields
    if not subtask_id:
        return "REJECTED: write_role_handover requires a non-empty subtask_id."
    if not from_role:
        return "REJECTED: write_role_handover requires a non-empty from_role."
    if not isinstance(brief, dict) or not brief:
        return "REJECTED: brief is required and must be a JSON object."
    summary = (brief.get("summary") or "").strip()
    outcome = brief.get("outcome")
    if not summary or not outcome:
        return (
            "REJECTED: empty role-handover. brief.summary and brief.outcome "
            "are mandatory."
        )

    # V1 — summary length
    if len(summary) > _HANDOVER_SUMMARY_MAX:
        return (
            f"REJECTED: brief.summary is {len(summary)} chars; "
            f"max {_HANDOVER_SUMMARY_MAX}. Compress — refs scale, prose "
            "doesn't. Move depth to cortex_refs."
        )

    # V2 — no fenced code anywhere a human-readable string is allowed
    if _has_fenced_code(summary):
        return (
            "REJECTED: fenced code block in brief.summary. Role-handover "
            "never inlines source. Emit a cortex_ref (path + line range) "
            "instead."
        )
    decisions = brief.get("decisions") or []
    if not isinstance(decisions, list):
        return "REJECTED: brief.decisions must be an array."
    if len(decisions) > _HANDOVER_DECISIONS_HARDCAP:
        return (
            f"REJECTED: brief.decisions has {len(decisions)} rows; max "
            f"{_HANDOVER_DECISIONS_HARDCAP}. If you need more, split the "
            "subtask — handovers are pointers, not transcripts."
        )
    for i, d in enumerate(decisions):
        if not isinstance(d, dict):
            return f"REJECTED: brief.decisions[{i}] must be an object."
        decision = d.get("decision") or ""
        rationale = d.get("rationale") or ""
        if not decision or not rationale:
            return (
                f"REJECTED: brief.decisions[{i}] requires both 'decision' "
                "and 'rationale'."
            )
        if _has_fenced_code(decision) or _has_fenced_code(rationale):
            return (
                f"REJECTED: fenced code block in brief.decisions[{i}]. "
                "Role-handover never inlines source. Emit a cortex_ref."
            )
        # V7 — rationale cap
        if len(decision) > _HANDOVER_DECISION_MAX:
            return (
                f"REJECTED: brief.decisions[{i}].decision exceeds "
                f"{_HANDOVER_DECISION_MAX} chars. Split or compress."
            )
        if len(rationale) > _HANDOVER_RATIONALE_MAX:
            return (
                f"REJECTED: brief.decisions[{i}].rationale exceeds "
                f"{_HANDOVER_RATIONALE_MAX} chars. Split or compress."
            )

    open_questions = brief.get("open_questions") or []
    if not isinstance(open_questions, list):
        return "REJECTED: brief.open_questions must be an array of strings."
    for i, q in enumerate(open_questions):
        if not isinstance(q, str):
            return f"REJECTED: brief.open_questions[{i}] must be a string."
        if _has_fenced_code(q):
            return (
                f"REJECTED: fenced code block in brief.open_questions[{i}]. "
                "Role-handover never inlines source."
            )
        if len(q) > _HANDOVER_QUESTION_MAX:
            return (
                f"REJECTED: brief.open_questions[{i}] exceeds "
                f"{_HANDOVER_QUESTION_MAX} chars. Split or compress."
            )

    # V8 — failed → ≥1 open_question (recovery path)
    if outcome == "failed" and not open_questions:
        return (
            "REJECTED: outcome='failed' must include at least one "
            "open_question describing the recovery path."
        )

    # V9 (wave-3 G7) — produced_files structural validation. The brief is
    # the canonical source for files this subtask materially changed; V3's
    # artifacts/ scan only sees orchestrator-bookkeeping output, never the
    # project repo where real code lands.
    produced_files = brief.get("produced_files") or []
    if not isinstance(produced_files, list):
        return "REJECTED: brief.produced_files must be an array of strings."
    if len(produced_files) > 100:
        return (
            f"REJECTED: brief.produced_files has {len(produced_files)} entries; "
            "max 100. If you really changed more files, split the subtask."
        )
    seen_produced: set[str] = set()
    for i, p in enumerate(produced_files):
        if not isinstance(p, str):
            return f"REJECTED: brief.produced_files[{i}] must be a string."
        p_stripped = p.strip()
        if not p_stripped:
            return f"REJECTED: brief.produced_files[{i}] is empty."
        if len(p_stripped) > 500:
            return (
                f"REJECTED: brief.produced_files[{i}] exceeds 500 chars. "
                "Use absolute paths, not URLs or descriptions."
            )
        if _has_fenced_code(p_stripped):
            return f"REJECTED: fenced code block in brief.produced_files[{i}]."
        if not p_stripped.startswith("/"):
            return (
                f"REJECTED: brief.produced_files[{i}]='{p_stripped}' must be "
                "an absolute path (start with '/'). Relative paths drift "
                "across subagents."
            )
        if p_stripped in seen_produced:
            return (
                f"REJECTED: brief.produced_files[{i}]='{p_stripped}' is a "
                "duplicate. List each path once."
            )
        seen_produced.add(p_stripped)

    # V11 (umbrella audit 2026-05-14) — produced_files disk-existence. V9
    # enforces shape; V11 enforces that the files actually exist on disk at
    # handover-write time. Without this, a subagent could pass the verify
    # gate (Stream A + Stream B both shipped, outcome='success') while
    # listing phantom paths — downstream then consumes ghost cortex_refs.
    # Three independent auditors (handover / context-preservation /
    # pitfalls) flagged this as the single highest-leverage gap.
    missing_files: list[str] = []
    for p in produced_files:
        p_stripped = p.strip()
        if not os.path.exists(p_stripped):
            missing_files.append(p_stripped)
    if missing_files:
        sample = "; ".join(missing_files[:5])
        return (
            f"REJECTED: {len(missing_files)} brief.produced_files path(s) do "
            f"not exist on disk: {sample}"
            f"{' (and more)' if len(missing_files) > 5 else ''}. "
            "produced_files must point at files that exist when the handover "
            "is written. Either remove phantom paths or write the files first."
        )

    # V10 (wave-4 G9) — contracts structural validation. Fenced code blocks
    # are EXPLICITLY allowed in `schema` only — that's the field's purpose.
    # All other string fields stay text-only.
    contracts = brief.get("contracts") or []
    if not isinstance(contracts, list):
        return "REJECTED: brief.contracts must be an array of objects."
    if len(contracts) > 5:
        return (
            f"REJECTED: brief.contracts has {len(contracts)} entries; max 5. "
            "If you need more typed contracts, split the subtask — handovers "
            "are pointers, not full specs."
        )
    seen_contract_names: set[str] = set()
    _CONTRACT_KINDS = {"api", "schema", "type", "envelope", "event", "config"}
    for i, c in enumerate(contracts):
        if not isinstance(c, dict):
            return f"REJECTED: brief.contracts[{i}] must be an object."
        name = (c.get("name") or "").strip()
        kind = (c.get("kind") or "").strip()
        schema_body = c.get("schema") or ""
        notes = (c.get("notes") or "").strip()
        if not name:
            return f"REJECTED: brief.contracts[{i}].name is required."
        if len(name) > 120:
            return f"REJECTED: brief.contracts[{i}].name exceeds 120 chars."
        if _has_fenced_code(name):
            return f"REJECTED: fenced code block in brief.contracts[{i}].name."
        if name in seen_contract_names:
            return (
                f"REJECTED: brief.contracts[{i}].name='{name}' is a duplicate. "
                "Each contract must have a unique name."
            )
        seen_contract_names.add(name)
        if kind not in _CONTRACT_KINDS:
            return (
                f"REJECTED: brief.contracts[{i}].kind='{kind}' must be one of "
                f"{sorted(_CONTRACT_KINDS)}."
            )
        if not isinstance(schema_body, str) or not schema_body.strip():
            return f"REJECTED: brief.contracts[{i}].schema is required (non-empty string)."
        if len(schema_body) > 4000:
            return (
                f"REJECTED: brief.contracts[{i}].schema is {len(schema_body)} "
                "chars; max 4000. Split, or move bulk to a cortex_ref and "
                "keep only the contract surface here."
            )
        # `schema` allows fenced code (that's its purpose); `notes` does not.
        if notes and len(notes) > 400:
            return f"REJECTED: brief.contracts[{i}].notes exceeds 400 chars."
        if notes and _has_fenced_code(notes):
            return (
                f"REJECTED: fenced code block in brief.contracts[{i}].notes. "
                "Code goes in .schema only."
            )

    # V4/V5 — cortex refs structural validation
    refs = arguments.get("cortex_refs") or []
    if not isinstance(refs, list):
        return "REJECTED: cortex_refs must be an array of objects."

    try:
        from okuro.cortex import resolve_path as _resolve_path
    except Exception as exc:
        _guard_failed("cortex_resolve_path_import", exc)
        _resolve_path = None  # cortex unavailable — V4 soft-passes

    seen_paths: set[str] = set()
    for i, ref in enumerate(refs):
        if not isinstance(ref, dict):
            return f"REJECTED: cortex_refs[{i}] must be an object."
        path = (ref.get("path") or "").strip()
        if not path:
            return (
                f"REJECTED: cortex_refs[{i}].path is required (absolute or "
                "registered-root-relative)."
            )
        s = ref.get("start_line")
        e = ref.get("end_line")
        if not isinstance(s, int) or not isinstance(e, int):
            return (
                f"REJECTED: cortex_refs[{i}] requires integer start_line "
                "and end_line."
            )
        if s < 1 or e < 1:
            return (
                f"REJECTED: cortex_refs[{i}] line numbers must be >= 1 "
                f"(got start_line={s}, end_line={e})."
            )
        if s > e:
            return (
                f"REJECTED: cortex_refs[{i}] has start_line ({s}) > "
                f"end_line ({e})."
            )
        purpose = (ref.get("purpose") or "").strip()
        if not purpose:
            return (
                f"REJECTED: cortex_refs[{i}].purpose is required (<=160 chars)."
            )
        if len(purpose) > _HANDOVER_PURPOSE_MAX:
            return (
                f"REJECTED: cortex_refs[{i}].purpose exceeds "
                f"{_HANDOVER_PURPOSE_MAX} chars."
            )
        if _has_fenced_code(purpose):
            return (
                f"REJECTED: fenced code block in cortex_refs[{i}].purpose. "
                "Role-handover never inlines source."
            )
        # V4 — path must resolve under a registered cortex root.
        if _resolve_path is not None:
            try:
                resolved = _resolve_path(path)
            except FileNotFoundError:
                return (
                    f"REJECTED: cortex_refs[{i}].path '{path}' did not "
                    "resolve under any registered cortex root. Either fix "
                    "the path or remove the ref."
                )
            seen_paths.add(str(resolved))
        else:
            seen_paths.add(path)

    # V3 — every produced file must have at least one cortex_ref. Best-effort
    # state-read; soft-passes if orchestrator state isn't accessible.
    produced = _produced_files_for(subtask_id)
    if produced and not refs:
        return (
            f"REJECTED: subtask {subtask_id} produced files under artifacts/ "
            "but cortex_refs is empty. Add at least one ref (path + "
            "start_line + end_line + purpose) for each file the next "
            "subagent must read."
        )
    if produced and refs:
        # Lenient match — produced file path must be a prefix of, or contained
        # in, at least one ref's resolved path. This avoids FN/FP from path
        # canonicalisation differences (relative vs absolute, symlinks).
        unmatched = []
        for produced_path in produced:
            matched = any(
                ref_path == produced_path
                or produced_path.endswith(ref_path)
                or ref_path.endswith(produced_path)
                for ref_path in seen_paths
            )
            if not matched:
                unmatched.append(produced_path)
        # Don't hard-fail on partial coverage — that's the producer's call to
        # bundle related files. Hard-fail only when zero refs cover anything.
        if seen_paths and len(unmatched) == len(produced):
            return (
                f"REJECTED: subtask {subtask_id} produced files under "
                "artifacts/ but none of cortex_refs[*].path covers them. "
                "At least one ref must point at the produced work."
            )

    # KG edges structural sanity (no validator number — extension of V6).
    kg_edges = arguments.get("kg_edges") or []
    if not isinstance(kg_edges, list):
        return "REJECTED: kg_edges must be an array of objects."
    for i, edge in enumerate(kg_edges):
        if not isinstance(edge, dict):
            return f"REJECTED: kg_edges[{i}] must be an object."
        for col in ("subject", "predicate", "object"):
            v = (edge.get(col) or "").strip()
            if not v:
                return (
                    f"REJECTED: kg_edges[{i}].{col} is required."
                )
            if _has_fenced_code(v):
                return (
                    f"REJECTED: fenced code block in kg_edges[{i}].{col}."
                )

    return None
