# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Compressor — distill task_events + task.adrs + recent file
#   diff into a decision-trace the next serial subagent reads instead of
#   raw history. M2 primitive. Model-agnostic via bridge_invoke.
# index: imports | _recent_git_diff | _build_prompt | run_compression |
#   AGENT_HEADER_END -->
"""Compressor — task-event log distillation for serial pipelines.

Runs between long serial steps (port loops, drift checkpoints,
multi-step refactors). Reads the task event log (M2), task.adrs (M1),
and a bounded recent git diff for task.project_path. Calls bridge_invoke
(Sonnet default — cheap, not Opus) to produce a 2-5K-token
decision-trace. Persists the trace as an artifact (kind='plan') and
appends a `compression` event linking the artifact id + the seq range
it covers.

Called by the engine BEFORE dispatching the next serial subtask in
a gates_enabled or serialize-on-phase task. NOT a CLI subagent — the
compressor runs in-process to avoid per-spawn bootstrap overhead.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


_PROMPT_TEMPLATE = """You are the COMPRESSOR. Your job: produce a strictly-structured decision-trace summarising the orchestrator task's event log so the next serial subagent inherits the context without reading raw history.

# TASK
- id: {task_id}
- description: {task_description}
- project_path: {project_path}

# LOCKED ADRs (user-resolved gates — verbatim, cannot be overridden)
{adrs_block}

# EVENT LOG (seq {seq_from} → {seq_to})
{events_block}

# PRIOR COMPRESSION (if any — your output supersedes it)
{prior_compression}

# RECENT FILE ACTIVITY (git diff --stat against HEAD~5 in project_path; capped)
{git_block}

# OUTPUT — emit ONLY the markdown body in this exact shape, nothing else:

## Locked ADRs
| # | Topic | Choice | Why |
|---|-------|--------|-----|

## Active Decisions (from event log)
| Seq | Subtask | Topic | Choice | Rationale | Superseded-from |
|-----|---------|-------|--------|-----------|------------------|

## Contracts in Force
(per contract — heading "### `<name>` (`<kind>`)" then schema verbatim, optional "_Notes:_ ...")

## Open Gaps
(bullets — "<summary> — affects: <list>")

## Open Questions
(bullets — "<question> [BLOCKING|non-blocking]")

## Recent File Activity
(bullets ≤20 — "`<path>` — <last subtask>")

Rules:
- Active decisions ONLY. If a topic was superseded, list the active head and put "was X" in the Superseded-from column. Drop fully-superseded chains.
- NEVER invent. If the log says nothing about a section, output the heading + "_(none)_".
- NO prose outside the blocks. NO summary. NO commentary.
- Budget: keep total output under 5000 tokens.
"""


def _recent_git_diff(project_path: str | None, max_chars: int = 3000) -> str:
    """Return `git diff --stat HEAD~5` for project_path, capped.

    Best-effort: missing repo / no commits / shell errors all collapse
    to an empty marker. Never raises into the caller.
    """
    if not project_path:
        return "_(no project_path on task)_"
    p = Path(project_path)
    if not p.exists() or not (p / ".git").exists():
        return "_(project_path is not a git repo)_"
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~5"],
            cwd=str(p),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return f"_(git diff failed: {result.stderr.strip()[:120]})_"
        out = result.stdout.strip()
        if not out:
            return "_(no changes in last 5 commits)_"
        return out[:max_chars]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return f"_(git diff unavailable: {exc!s})_"


def _format_adrs(adrs: list[dict]) -> str:
    if not adrs:
        return "_(none — no user-resolved gates)_"
    lines = []
    for i, a in enumerate(adrs, 1):
        topic = (a.get("prompt") or "?").replace("\n", " ").strip()
        choice = (a.get("selected_label") or a.get("selected_option_id") or "?").strip()
        rationale = (a.get("selected_rationale") or "_(no rationale)_").replace("\n", " ").strip()
        lines.append(f"{i}. **{topic}** → `{choice}` — {rationale}")
    return "\n".join(lines)


def _format_events(events: list[dict]) -> str:
    if not events:
        return "_(no events in range)_"
    lines = []
    for e in events:
        seq = e.get("seq", "?")
        et = e.get("event_type", "?")
        sub = e.get("subtask_id", "?")
        role = e.get("from_role") or "?"
        body = e.get("body") or {}
        body_str = ", ".join(f"{k}={v!r}"[:200] for k, v in body.items() if k != "schema_body")
        if "schema_body" in body:
            body_str += f" | schema_body=<{len(body.get('schema_body', ''))} chars>"
        lines.append(f"- seq={seq} type={et} subtask={sub} role={role} :: {body_str}")
    return "\n".join(lines)


def _build_prompt(
    *,
    task_id: str,
    task_description: str,
    project_path: str,
    adrs: list[dict],
    events: list[dict],
    prior_compression_summary: str,
    seq_from: int,
    seq_to: int,
) -> str:
    return _PROMPT_TEMPLATE.format(
        task_id=task_id,
        task_description=task_description[:400],
        project_path=project_path or "_(none)_",
        adrs_block=_format_adrs(adrs),
        events_block=_format_events(events),
        prior_compression=prior_compression_summary or "_(none)_",
        git_block=_recent_git_diff(project_path),
        seq_from=seq_from,
        seq_to=seq_to,
    )


def run_compression(
    *,
    task_id: str,
    task_description: str,
    project_path: str,
    adrs: list[dict],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 180,
) -> dict:
    """Compress the task event log into a decision-trace.

    Reads events since the last compression event's `covers_seq_to`,
    invokes bridge with Sonnet default, persists the trace as an
    artifact (kind='plan'), appends a `compression` event referencing
    the artifact.

    Returns:
        dict {ok: bool, event_id: str|None, artifact_id: str|None,
              error: str, prompt_chars: int, output_chars: int}
    """
    from okuro.sense.task_events import (
        append_event, list_events, latest_compression,
    )

    prior = latest_compression(task_id=task_id)
    since_seq = 0
    prior_summary = ""
    if prior:
        prior_body = prior.get("body") or {}
        since_seq = int(prior_body.get("covers_seq_to") or 0)
        prior_summary = prior_body.get("summary") or ""

    events = list_events(task_id=task_id, since_seq=since_seq, limit=500)
    if not events:
        return {
            "ok": False,
            "event_id": None,
            "artifact_id": None,
            "error": f"no new events since seq={since_seq}; nothing to compress",
            "prompt_chars": 0,
            "output_chars": 0,
        }

    seq_from = events[0]["seq"]
    seq_to = events[-1]["seq"]

    prompt = _build_prompt(
        task_id=task_id,
        task_description=task_description,
        project_path=project_path,
        adrs=adrs,
        events=events,
        prior_compression_summary=prior_summary,
        seq_from=seq_from,
        seq_to=seq_to,
    )

    # Bridge invoke — cheap default = Sonnet on Claude. Caller may force
    # provider/model for testing or budget reasons.
    from okuro.bridge.invoke import invoke as bridge_invoke

    result = bridge_invoke(
        prompt=prompt,
        capability=None,
        provider=provider or "claude",
        model=model or "sonnet",
        timeout=timeout,
    )

    if not result.get("success"):
        return {
            "ok": False,
            "event_id": None,
            "artifact_id": None,
            "error": result.get("error") or "bridge_invoke failed",
            "prompt_chars": len(prompt),
            "output_chars": 0,
        }

    trace_md = (result.get("output") or "").strip()
    if not trace_md:
        return {
            "ok": False,
            "event_id": None,
            "artifact_id": None,
            "error": "bridge returned empty output",
            "prompt_chars": len(prompt),
            "output_chars": 0,
        }

    # Persist the trace as an artifact (kind='plan' — pre-execution
    # context for the next subagent).
    from okuro.sense.artifacts import artifact_write

    artifact_id = artifact_write(
        kind="plan",
        title=f"Decision trace — {task_id} (seq {seq_from}–{seq_to})",
        body=trace_md,
        summary=f"Compressor decision-trace covering events {seq_from}–{seq_to}",
        task_id=task_id,
        subtask_id="compressor",
        created_by="compressor",
        confidence=0.85,
    )

    # Append compression event so the next dispatch sees it.
    event_id = append_event(
        task_id=task_id,
        subtask_id="compressor",
        from_role="compressor",
        event_type="compression",
        body={
            "artifact_id": artifact_id,
            "covers_seq_from": seq_from,
            "covers_seq_to": seq_to,
            "summary": trace_md.split("\n", 1)[0][:600] or "decision trace",
        },
        confidence=0.85,
        created_by="compressor",
        adrs=adrs,
    )

    return {
        "ok": True,
        "event_id": event_id,
        "artifact_id": artifact_id,
        "error": "",
        "prompt_chars": len(prompt),
        "output_chars": len(trace_md),
    }
