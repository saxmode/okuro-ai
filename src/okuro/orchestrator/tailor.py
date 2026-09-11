# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Tailor — translate a PASSED reviewer-shaped Stream B artifact into
#   a plain-language, user-voiced deliverable. Archives the reviewer body as a
#   process-audience evidence copy; the user tab then shows only the tailored
#   form. Sibling of compressor.py. Model-agnostic via bridge_invoke.
# index: imports | _load_user_voice | _build_prompt | _tailor_one | run_tailor
# AGENT_HEADER_END -->
"""Tailor — reviewer-body -> user-facing deliverable translation (Stream B).

The M3 reviewer evidentiary contract (dispatcher `_render_evidentiary_contract`)
forces every subagent report/plan body into a machine-verifiable shape: `## AC`
anchors, ```bash``` captures, exit codes, `emit_task_event(gap=...)` snippets.
That body is what the reviewer judges — and, until this pass existed, exactly
what the web UI ArtifactsViewer showed the *user* verbatim. Two audiences, one
body: the user got the reviewer's evidence dump.

This pass runs AFTER a subtask passes review and finalizes (engine
`_finalize_subtask`). For each `audience="user"` report/plan artifact it:

  1. reads the reviewer-shaped body,
  2. rewrites it in the user's own communication voice via bridge_invoke
     (Sonnet default — cheap, in-process, no CLI spawn), then
  3. archives the original reviewer body as an `audience="process"` evidence
     copy (visible in the UI "Internal" tab for audit / reviewer rerun) and
  4. writes the tailored body as the same kind, which auto-supersedes the raw
     row (artifacts.py G13 conflict rule) so the user tab shows only the clean
     version.

Rerun-safe: `rerun_subtasks_from_verdict` re-DISPATCHES a failed subtask, which
produces a fresh reviewer-shaped raw artifact (auto-superseding any tailored
row) that the reviewer judges — the reviewer never sees a tailored body.

Best-effort by contract: any failure leaves the raw artifact untouched and
user-visible rather than dropping the deliverable. The caller swallows
exceptions; this module also degrades internally.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("okuro.orchestrator.tailor")

# Only prose deliverables carry the reviewer scaffolding the user trips over.
# `evidence` is raw-by-design (a report cites it) and is left alone.
_TAILORABLE_KINDS = frozenset({"report", "plan"})

# Author tag on tailor output — also the idempotency guard: a report whose
# current row is already authored by the tailor is not re-tailored.
_TAILOR_AUTHOR = "tailor"


_PROMPT_TEMPLATE = """You are the TAILOR — an editor who rewrites a VERIFIED internal \
deliverable into the form its reader actually wants. The reader is the person \
who runs this orchestrator; rewrite in THEIR voice, below.

The input below already passed automated review. It is written for a machine \
reviewer: `## AC1` acceptance-criterion anchors, fenced ```bash``` command \
captures, exit codes, `emit_task_event(...)` snippets, internal principle codes \
(e.g. DP10), subtask ids. The reader does not want any of that scaffolding.

# YOUR JOB
Rewrite the deliverable so the reader gets the outcome, not the audit trail.
- Lead with the answer / result. No preamble, no "This report...".
- Prefer short bullet lists and tables over prose.
- Replace pasted shell output with a one-line plain-language result
  ("Verified: migration applied, 0 errors") — keep the FACT, drop the dump.
- Drop `## AC` anchors, exit-code lines, `emit_task_event` code, and internal
  codes/ids. Translate any jargon into plain language.
- Keep the deliverable's real content: decisions, numbers, file paths the
  reader needs, trade-offs, and any recommendation.

# HARD CONSTRAINTS — do not violate
- Do NOT invent facts, results, numbers, or claims not present in the input.
- If the input shows a failure, a gap, an open question, or an unverified
  item, PRESERVE it honestly — do not paper over it.
- This is a WORDING + STRUCTURE pass, not a re-analysis. Same truth, clearer form.
- Output ONLY the rewritten markdown. No commentary about what you changed.

{voice_block}
# DELIVERABLE TITLE
{title}

# DELIVERABLE BODY (reviewer-shaped — rewrite this)
{body}
"""


def _load_user_voice() -> str:
    """Render the user's communication preferences as a prompt block.

    Reads the same profile source the dispatcher's `_build_user_format_block`
    uses (`okuro.yu.profile.get_profile_raw`) so the tailored artifact matches
    the voice the user sees everywhere else. Returns "" when the profile is
    unavailable so the pass still runs with generic clarity guidance.
    """
    try:
        from okuro.yu.profile import get_profile_raw

        profile = get_profile_raw() or {}
    except Exception:
        return ""

    if not isinstance(profile, dict) or not profile:
        return ""

    comm = profile.get("communication") if isinstance(profile.get("communication"), dict) else {}
    fmt = comm.get("format_preferences") if isinstance(comm.get("format_preferences"), dict) else {}

    def _texts(items, limit: int = 5) -> list[str]:
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for item in items:
            if isinstance(item, str):
                t = item.strip()
            elif isinstance(item, dict):
                t = str(item.get("rule", "")).strip()
            else:
                t = ""
            if t and t not in out:
                out.append(t)
            if len(out) >= limit:
                break
        return out

    do_lines: list[str] = []
    do_lines.extend(_texts(comm.get("patterns")))
    do_lines.extend(t for t in _texts(fmt.get("preferred")) if t not in do_lines)

    avoid_lines: list[str] = []
    avoid_lines.extend(_texts(fmt.get("avoid")))
    avoid_lines.extend(t for t in _texts(comm.get("pet_peeves")) if t not in avoid_lines)

    if not do_lines and not avoid_lines:
        return ""

    parts = ["# READER VOICE — write to these preferences"]
    if do_lines:
        parts.append("Do:")
        parts.extend(f"- {t}" for t in do_lines)
    if avoid_lines:
        parts.append("Avoid:")
        parts.extend(f"- {t}" for t in avoid_lines)
    parts.append("")
    return "\n".join(parts)


def _build_prompt(*, title: str, body: str, voice_block: str) -> str:
    return _PROMPT_TEMPLATE.format(
        title=(title or "").strip() or "(untitled)",
        body=body,
        voice_block=(voice_block + "\n") if voice_block else "",
    )


def _tailor_one(
    row: dict,
    *,
    task_id: str,
    subtask_id: str,
    voice_block: str,
    provider: Optional[str],
    model: Optional[str],
    timeout: int,
) -> Optional[str]:
    """Tailor a single raw artifact. Returns the tailored artifact id, or None
    when the raw was left untouched (empty body, bridge failure, empty output).
    """
    from okuro.sense.artifacts import artifact_get, artifact_write
    from okuro.bridge.invoke import invoke as bridge_invoke

    raw_id = row.get("id")
    kind = row.get("kind") or "report"
    full = artifact_get(raw_id, include_body=True) or {}
    reviewer_body = (full.get("body") or "").strip()
    title = full.get("title") or row.get("title") or "Deliverable"
    if not reviewer_body:
        log.debug("[%s] tailor: raw %s has empty body — skip", subtask_id, raw_id)
        return None

    prompt = _build_prompt(title=title, body=reviewer_body, voice_block=voice_block)
    result = bridge_invoke(
        prompt=prompt,
        capability=None,
        provider=provider or "claude",
        model=model or "sonnet",
        timeout=timeout,
    )
    if not result.get("success"):
        log.warning("[%s] tailor: bridge_invoke failed for %s: %s",
                    subtask_id, raw_id, result.get("error"))
        return None

    tailored_body = (result.get("output") or "").strip()
    if not tailored_body:
        log.warning("[%s] tailor: bridge returned empty output for %s", subtask_id, raw_id)
        return None

    # 1) Archive the reviewer body as a process-audience evidence copy so the
    #    UI "Internal" tab and any reviewer rerun keep the machine-verifiable
    #    proof. Suffixed subtask id avoids colliding with (and superseding) a
    #    real evidence artifact the subagent may have written.
    artifact_write(
        kind="evidence",
        title=f"{title} — review evidence (raw)",
        body=reviewer_body,
        summary=(full.get("summary") or "")[:600] or f"Reviewer-shaped source for {raw_id}",
        task_id=task_id,
        subtask_id=f"{subtask_id}#src",
        parent_id=raw_id,
        created_by=_TAILOR_AUTHOR,
        audience="process",
        confidence=0.8,
    )

    # 2) Write the tailored body as the same kind + real subtask id. The G13
    #    conflict rule in artifact_write auto-supersedes the raw row, so the
    #    user tab shows only this clean version.
    tailored_id = artifact_write(
        kind=kind if kind in _TAILORABLE_KINDS else "report",
        title=title,
        body=tailored_body,
        summary=(full.get("summary") or "")[:600] or None,
        task_id=task_id,
        subtask_id=subtask_id,
        created_by=_TAILOR_AUTHOR,
        audience="user",
        confidence=0.8,
    )
    if isinstance(tailored_id, str) and tailored_id.startswith("REJECTED"):
        log.warning("[%s] tailor: artifact_write rejected: %s", subtask_id, tailored_id)
        return None
    return tailored_id


def run_tailor(
    *,
    task_id: str,
    subtask_id: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 120,
) -> dict:
    """Translate every user-facing report/plan artifact of a passed subtask.

    Args:
        task_id, subtask_id: identify the finalized subtask's Stream B rows.
        provider, model: bridge override (default claude/sonnet — cheap).
        timeout: per-artifact bridge timeout (seconds).

    Returns:
        dict {ok: bool, tailored: list[str], skipped: int, error: str}
        - ``tailored`` = ids of the tailored artifacts written.
        - ``skipped``  = raw artifacts left untouched (already tailored, empty,
                          or a per-artifact bridge failure).
    """
    try:
        from okuro.sense.artifacts import artifact_list
    except Exception as exc:  # pragma: no cover - import guard
        return {"ok": False, "tailored": [], "skipped": 0, "error": f"import failed: {exc}"}

    try:
        rows = artifact_list(
            task_id=task_id,
            subtask_id=subtask_id,
            audience="user",
            include_superseded=False,
            limit=50,
        )
    except Exception as exc:
        return {"ok": False, "tailored": [], "skipped": 0, "error": f"artifact_list failed: {exc}"}

    candidates = [
        r for r in rows
        if (r.get("kind") in _TAILORABLE_KINDS)
        and (r.get("created_by") != _TAILOR_AUTHOR)  # idempotency guard
    ]
    if not candidates:
        return {"ok": True, "tailored": [], "skipped": 0, "error": ""}

    voice_block = _load_user_voice()
    tailored: list[str] = []
    skipped = 0
    for row in candidates:
        try:
            tid = _tailor_one(
                row,
                task_id=task_id,
                subtask_id=subtask_id,
                voice_block=voice_block,
                provider=provider,
                model=model,
                timeout=timeout,
            )
        except Exception as exc:
            log.warning("[%s] tailor: raw %s raised: %s", subtask_id, row.get("id"), exc)
            tid = None
        if tid:
            tailored.append(tid)
        else:
            skipped += 1

    return {"ok": True, "tailored": tailored, "skipped": skipped, "error": ""}
