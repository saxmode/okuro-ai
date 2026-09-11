# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Continuation Advisor — LLM-judged "you're almost done" signals.
# index:
#   class Continuation
#   def find_continuations
#   def _list_active_projects
#   def _build_project_bundle
#   def _ask_llm
#   def _parse_response
#   def _emit_signal
# AGENT_HEADER_END -->
"""Continuation Advisor — LLM-judged "you're almost done" signals.

Where the P1 heuristic scanners surface STALE state ("X is 25d old"),
this advisor surfaces NEAR-COMPLETE work ("X is 90% built, just needs
the JSON templates"). It pulls a cross-source bundle per project
(progress, recent commits, open todos, open thoughts), hands it to
claude via the bridge, and emits one signal per project whose
readiness clears a threshold.

Output goes into the same `signals` table with
`evidence.bucket='continuation'` so the Now-page UI can route it into
the prime real-estate column above the (demoted) stale bucket.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_READINESS_FLOOR = 70
_MAX_PROJECTS_PER_RUN = 10
_PER_PROJECT_TIMEOUT = 180  # seconds for one LLM call — claude CLI on a
# 3-4k-token bundle measured 60-110s; the largest workspace repo timed out at 120s
# with a denser bundle, so allow generous headroom.
_PROVIDER = "claude"

_SYSTEM_PROMPT = """\
You are a continuation advisor for an AI builder. Given a cross-source
bundle for ONE project (progress, recent commits, open todos, open
thoughts), identify whether that project is NEAR-COMPLETE and gated
by a SINGLE concrete step.

Output strictly ONE JSON object, no prose, no markdown fence:
{
  "readiness_percent": <int 0..100>,
  "blocking_step": "<one imperative sentence, max 80 chars, verb first>" or null,
  "invitation": "<one sentence naming the project and what is already done>" or null,
  "evidence_refs": ["<short ref like file path or commit subject>", ...]
}

Rules:
- Only set non-null blocking_step / invitation when readiness >= 70 AND a SINGLE clearly-identifiable next step would ship the work.
- Do NOT fabricate. If the bundle is too sparse or scattered, return readiness=0 with everything null.
- blocking_step is the headline the user reads in their inbox. Lead with the
  action, name the thing, no wind-up.
    good: "Re-derive the embed pin from live config and add a CI dim assert"
    bad:  "It looks like the embedding work is nearly there — shall we finish it?"
- blocking_step must NOT be a question. Never ask whether to proceed, and never
  offer to "find a strategy" — the user decides what to do with it.
- Invitation must be specific (mention the project name and the concrete remainder), not generic.
- Keep evidence_refs to 3-5 short items pulled directly from the bundle.
"""


@dataclass
class Continuation:
    """Parsed LLM output per project."""

    project: str
    readiness_percent: int
    blocking_step: str | None
    invitation: str | None
    evidence_refs: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return (
            self.readiness_percent >= _READINESS_FLOOR
            and bool(self.blocking_step)
            and bool(self.invitation)
        )


def find_continuations(
    *,
    max_projects: int = _MAX_PROJECTS_PER_RUN,
    timeout: int = _PER_PROJECT_TIMEOUT,
    provider: str = _PROVIDER,
) -> dict:
    """Walk active projects, ask claude per project, emit continuation signals.

    Returns a summary dict with per-project verdicts and tally of
    written signals + LLM errors. Safe to call repeatedly: dedupe on
    `source_ref='continuation:{project}'` against open signals means a
    project gets at most one open continuation at a time.
    """
    from okuro.db import get_db
    from okuro.sense import signals as signals_svc

    db = get_db()
    open_refs = _load_open_continuation_refs(db)

    projects = _list_active_projects(db, limit=max_projects)
    log.info("continuation-advisor: %d candidate projects", len(projects))

    verdicts: dict[str, dict] = {}
    written = 0
    errors: dict[str, str] = {}

    for proj in projects:
        ref = f"continuation:{proj['id']}"
        if ref in open_refs:
            verdicts[proj["id"]] = {"status": "dedupe_skipped"}
            continue

        try:
            bundle = _build_project_bundle(db, proj)
        except Exception as exc:
            log.exception("bundle failed for %s: %s", proj["id"], exc)
            errors[proj["id"]] = f"bundle: {exc}"
            continue

        if not bundle.strip():
            verdicts[proj["id"]] = {"status": "empty_bundle"}
            continue

        try:
            raw = _ask_llm(bundle, provider=provider, timeout=timeout)
        except Exception as exc:
            log.exception("LLM call failed for %s: %s", proj["id"], exc)
            errors[proj["id"]] = f"llm: {exc}"
            continue

        cont = _parse_response(proj["id"], raw)
        if cont is None:
            verdicts[proj["id"]] = {"status": "unparseable"}
            errors[proj["id"]] = "parse: invalid JSON"
            continue

        verdicts[proj["id"]] = {
            "status": "actionable" if cont.actionable else "below_threshold",
            "readiness": cont.readiness_percent,
        }

        if not cont.actionable:
            continue

        try:
            _emit_signal(signals_svc, cont)
            written += 1
            open_refs.add(ref)
        except Exception as exc:
            log.exception("signal_add failed for %s: %s", proj["id"], exc)
            errors[proj["id"]] = f"emit: {exc}"

    log.info(
        "continuation-advisor: wrote=%d projects_scanned=%d errors=%d",
        written, len(projects), len(errors),
    )

    return {
        "written": written,
        "projects_scanned": len(projects),
        "verdicts": verdicts,
        "errors": errors,
    }


# ── candidate selection ────────────────────────────────────────────


def _list_active_projects(db, limit: int) -> list[dict]:
    """Return active projects ordered by most-recent progress timestamp.

    A project with no progress row is still returned (ordered last)
    because long-dormant projects can still have actionable code
    sitting on disk — the LLM decides whether the bundle is enough.
    """
    rows = db.fetchall(
        """SELECT p.id, p.name, p.path, p.description,
                  COALESCE(MAX(pr.updated_at), '1970-01-01') AS last_progress
             FROM projects p
        LEFT JOIN progress pr ON pr.project = p.id
            WHERE p.active = 1
         GROUP BY p.id
         ORDER BY last_progress DESC, p.name ASC
            LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in rows]


def _load_open_continuation_refs(db) -> set[str]:
    rows = db.fetchall(
        """SELECT source_ref FROM signals
           WHERE source = 'proactive' AND status = 'open'
             AND source_ref LIKE 'continuation:%'""",
    )
    return {r["source_ref"] for r in rows if r["source_ref"]}


# ── bundle assembly ────────────────────────────────────────────────


def _build_project_bundle(db, proj: dict) -> str:
    """Compact text bundle (<~3k tokens) describing this project."""
    pid = proj["id"]
    parts: list[str] = [
        f"## Project: {proj['name']} (id={pid})",
    ]
    if proj.get("description"):
        parts.append(proj["description"])

    # Last 5 progress entries
    progress = db.fetchall(
        """SELECT status, summary, next_steps, blockers, updated_at
             FROM progress WHERE project = ?
            ORDER BY updated_at DESC LIMIT 5""",
        (pid,),
    )
    if progress:
        parts.append("\n### Recent progress")
        for r in progress:
            line = f"- [{r['updated_at']}] {r['status']}: {r['summary']}"
            if r.get("next_steps"):
                line += f"  next={r['next_steps']}"
            if r.get("blockers"):
                line += f"  blockers={r['blockers']}"
            parts.append(line[:400])

    # Top 10 open todos
    todos = db.fetchall(
        """SELECT title, priority, created_at FROM todos
            WHERE project = ? AND status = 'open'
            ORDER BY priority ASC, created_at ASC LIMIT 10""",
        (pid,),
    )
    if todos:
        parts.append("\n### Open todos")
        for t in todos:
            parts.append(f"- P{t['priority']} · {t['title'][:140]}")

    # Open thoughts in last 60d (top 8)
    thoughts = db.fetchall(
        """SELECT content, created_at FROM thoughts
            WHERE project = ? AND status = 'open'
              AND created_at > datetime('now','-60 days')
            ORDER BY created_at DESC LIMIT 8""",
        (pid,),
    )
    if thoughts:
        parts.append("\n### Open thoughts (last 60d)")
        for th in thoughts:
            first_line = (th.get("content") or "").splitlines()[0:1]
            line = first_line[0] if first_line else ""
            parts.append(f"- {line[:180]}")

    # Recent commit subjects from the project path
    commits = _recent_commit_subjects(proj.get("path"), n=10)
    if commits:
        parts.append("\n### Recent commits")
        for c in commits:
            parts.append(f"- {c[:160]}")

    return "\n".join(parts)


def _recent_commit_subjects(path: str | None, n: int = 10) -> list[str]:
    """Best-effort `git log` against the project's working tree."""
    if not path:
        return []
    try:
        out = subprocess.run(
            ["git", "-C", path, "log", "--oneline", f"-{n}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if out.returncode != 0:
            return []
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception as exc:
        log.debug("git log failed for %s: %s", path, exc)
        return []


# ── LLM call + parse ───────────────────────────────────────────────


def _ask_llm(bundle: str, *, provider: str, timeout: int) -> str:
    """Send the bundle through bridge.invoke. Raises on transport failure."""
    from okuro.bridge import invoke

    result = invoke(
        prompt=bundle,
        system_prompt=_SYSTEM_PROMPT,
        provider=provider,
        timeout=timeout,
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected invoke return type: {type(result)}")
    if not result.get("success"):
        raise RuntimeError(result.get("error") or "invoke returned success=False")
    return str(result.get("output") or "")


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_BLOCK = re.compile(r"\{[^{}]*\"readiness_percent\".*?\}", re.DOTALL)


def _parse_response(project: str, raw: str) -> Continuation | None:
    """Tolerant parse — looks past code fences and trailing prose."""
    if not raw or not raw.strip():
        return None

    candidates: list[str] = []
    fence = _JSON_FENCE.search(raw)
    if fence:
        candidates.append(fence.group(1))
    block = _JSON_BLOCK.search(raw)
    if block:
        candidates.append(block.group(0))
    candidates.append(raw.strip())

    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            readiness = int(data.get("readiness_percent") or 0)
        except (TypeError, ValueError):
            readiness = 0
        return Continuation(
            project=project,
            readiness_percent=max(0, min(100, readiness)),
            blocking_step=_clean_str(data.get("blocking_step")),
            invitation=_clean_str(data.get("invitation")),
            evidence_refs=[
                str(x)[:160]
                for x in (data.get("evidence_refs") or [])
                if isinstance(x, (str, int, float))
            ][:10],
        )
    return None


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


# ── emit ──────────────────────────────────────────────────────────


def _emit_signal(signals_svc, cont: Continuation) -> None:
    """Write the continuation as a single proactive signal.

    ``summary`` is the row's TITLE in the inbox (reducer._pull_signals copies it
    verbatim), so it must be the answer, not the wind-up. It used to be
    ``invitation`` — a warm 300-char sentence ending in a canned question —
    while ``blocking_step``, the one concise imperative the same LLM call
    already returns, was routed to the hidden ``suggested_action``. okuro
    generated the right sentence and then buried it, on 39 live rows.
    The invitation is still carried in evidence for anyone who wants the prose.
    """
    severity = "warn" if cont.readiness_percent >= 90 else "info"
    signals_svc.signal_add(
        source="proactive",
        source_ref=f"continuation:{cont.project}",
        severity=severity,
        summary=(
            cont.blocking_step
            or cont.invitation
            or f"{cont.project}: continuation candidate"
        ),
        evidence={
            "scanner": "continuation_advisor",
            "bucket": "continuation",
            "source_kind": "project",
            "source_id": cont.project,
            "readiness_percent": cont.readiness_percent,
            "blocking_step": cont.blocking_step,
            "invitation": cont.invitation,
            "evidence_refs": cont.evidence_refs,
        },
        # Deliberately unset: summary now carries blocking_step, and
        # inbox_detail composes the body as `summary + "**Suggested action:**
        # " + suggested_action` — so repeating it here prints the same sentence
        # twice in one row. signal_promote falls back to summary when this is
        # absent, so the promote path is unaffected.
    )


__all__ = [
    "Continuation",
    "find_continuations",
]
