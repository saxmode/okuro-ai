# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: M3 deterministic check generator — reads plan spec + ADRs +
#   M2 event log, emits executable checks (term-consistency cross-grep,
#   link-integrity, file-exists, schema-assert, secret-name consistency).
#   NO LLM calls — anything that needs judgment belongs to the Critic.
# index: imports | data classes | check generators | check runners |
#   generate_checks | run_checks
# AGENT_HEADER_END -->
"""Deterministic checks for the M3 reviewer pipeline.

Runs BEFORE the Critic stage. Machine-checkable failures short-circuit
the LLM stages and feed straight into the verdict event with severity
``load_bearing``. Anything that survives this stage is escalated to the
Critic.

Check taxonomy (extend by adding a generator + a runner):

    term_consistency  — any term that appears as ``new_choice`` in a
                        ``supersedes`` event (or as the active choice in
                        a ``decision`` event / ADR) implies the PRIOR
                        term is now drift. Grep every deliverable for the
                        prior term; each hit is a finding.
    file_exists       — every path declared in ``subtask.outputs`` and
                        ``subtask.target_paths`` must exist on disk.
    link_integrity    — relative ``[text](path)`` / ``href="path"`` /
                        bare-path mentions in markdown/html deliverables
                        must resolve (within the project tree).
    schema_assert     — declared output extension must match the
                        deliverable on disk (``.md`` vs ``.html`` vs
                        ``.py``).
    secret_name       — every ``keyring_get("NAME")`` in deliverables
                        must use a name consistent with the active set
                        from ADRs / contracts (no aliased / typo'd
                        secrets).

Each check is a small dict so the runner stays generic. The runner is
pure too — no I/O outside read-only file access + the M2 event-log
query already done by ``generate_checks``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from okuro.db.engine import okuro_home

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Check:
    """One executable deterministic check.

    ``kind`` selects the runner; ``params`` is kind-specific. ``origin``
    explains WHY the check exists (which ADR / acceptance criterion /
    contract derived it) so a failure can be cited in the verdict.
    """
    kind: str
    params: dict
    origin: str
    severity: str = "load_bearing"  # load_bearing | cosmetic


@dataclass
class CheckResult:
    """One check outcome."""
    check: Check
    passed: bool
    findings: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def summary(self) -> str:
        if self.error:
            return f"[{self.check.kind}] runner error: {self.error}"
        if self.passed:
            return f"[{self.check.kind}] ok"
        return f"[{self.check.kind}] {len(self.findings)} finding(s)"


# ---------------------------------------------------------------------------
# Check generators — read plan + ADRs + events; emit Check objects
# ---------------------------------------------------------------------------


_TERM_SPLIT_RE = re.compile(r"[ \t,;:/()\[\]{}<>\"']+")
# Parenthetical asides in a decision choice carry incidental config detail,
# not the decision SUBJECT. We delete them before tokenizing so config nouns
# never become drift terms — see _candidate_terms for the full rationale.
_PARENTHETICAL_RE = re.compile(r"[(\[{][^)\]}]*[)\]}]")
_STOP_TERMS = {
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "on",
    "with", "as", "by", "via", "from", "be", "is", "are", "use",
    "using", "this", "that", "these", "those", "it", "its", "no",
    "yes", "true", "false", "null", "none", "any", "all",
    "static", "dynamic", "default", "custom", "new", "old",
    "pass", "fail", "good", "bad", "ok",
}


_DISTINCTIVE_TOKEN_RE = re.compile(r"\d|[-_.]|[a-z][A-Z]")


def _is_distinctive_token(tok: str) -> bool:
    """True when a token carries its own tech-term identity — a digit, a
    separator (``-_.``), or internal camelCase ("11ty", "next.js", "GraphQL").

    Generic dictionary words ("clean", "resource", "rest") and all-caps
    acronyms that also read as plain words return False, so they never become
    standalone drift terms — only the full multi-word phrase does.
    """
    return bool(_DISTINCTIVE_TOKEN_RE.search(tok))


# --- P1 root guard: terminology drift vs numeric/value supersession ---------
# A `decision`/`supersedes` whose new_choice is a NUMBER or a value sentence
# ("B_10Y=10,823 (within seq-75 …)") is a VALUE change, not a terminology
# rename. Feeding such values into term_consistency caused two regressions:
#   (1) `_candidate_terms("…10,823…")` split on the comma → "823" became a
#       standalone "drift term"; the runner then grepped every "823" in a
#       numbers table → spurious load-bearing FAIL → number-heavy review loop.
#   (2) the autofix substituted the *whole verbose new_choice sentence* for a
#       short/numeric token → sentence-for-token corruption + doubled bodies
#       (task-20260623-213417 subtasks 2.3/2.5, created_by='reviewer-autofix').
# Two guards, two purposes (DP10 — fix the mechanism for every numeric task):
#   • _is_term_candidate (lenient, term-side at pair generation) drops a term
#     that carries NO letter — i.e. a bare number — so numeric supersessions
#     never become drift CHECKS. Detection of real renames is untouched.
#   • _is_safe_autofix_token (strict, both sides at autofix application) only
#     auto-rewrites short canonical tokens; a verbose/numeric/value replacement
#     degrades to the legacy FAIL→re-dispatch path instead of corrupting text.
_TERM_LETTER_RE = re.compile(r"[A-Za-z]")
_VALUE_LIKE_RE = re.compile(r"[=%]|\d[.,]\d|\d{3,}")


# A MEASUREMENT is a value wearing a letter. "850w", "64gb", "80mm", "2nd" all
# pass the letter test above, and _is_distinctive_token promotes them to
# standalone drift terms because they contain a digit — that digit rule exists
# to keep real product names ("11ty", "next.js") checkable. The overlap is the
# root cause of the context-blind term matching: a superseding decision about
# ONE topic tokenises the old choice's whole prose, and every unrelated spec
# value in it ("… + populate 2nd rear 80mm exhaust mount …") becomes a
# load-bearing check that greps the deliverable for a NUMBER.
#
# Observed live on task-20260724-110216: '64gb' (matched a product-URL slug and
# a before/after comparison row) and '850w' (the REPLACEMENT PSU is itself
# 850W, so the term was never wrong) each blocked the gate until the user
# overrode it. A quantity is not terminology: value drift is what
# numeric_consistency and the numbers ledger check, and unlike term_consistency
# those are not load_bearing short-circuits.
#
# Shape: number, optional decimal, optional space, then a unit or an ordinal
# suffix — and nothing else. "11ty" survives (``ty`` is not a unit), as do all
# letter-initial tokens ("h100", "s3", "next.js"). Bare numbers are already
# rejected by the letter test.
_MEASUREMENT_TERM_RE = re.compile(
    r"^\d+(?:[.,]\d+)?\s*"
    r"(?:"
    r"[kmgtp]?(?:b|hz|w|wh|v|a|ah|bps|bit|byte|ohm|toks?)|"
    r"[kmgtp]?(?:t|ts)/?s|"
    r"[munp]?(?:m|s|f)|"
    r"cm|km|in|ft|min|hr?|d|"
    r"fps|dpi|ppi|px|pt|em|rem|rpm|"
    r"c|k|x|st|nd|rd|th|core|cores|ch|way|dpc|pin"
    r")$",
    re.IGNORECASE,
)


def _is_term_candidate(term: str) -> bool:
    """True when ``term`` can be a terminology drift subject.

    Two rejections, one rule — *a value is not a term*:

    * no letter at all → a bare number ("823" split out of "10,823");
    * a number plus a unit or ordinal → a measurement ("64gb", "850w",
      "80mm", "2nd"). See :data:`_MEASUREMENT_TERM_RE`.

    Both must be rejected at pair generation so no measurement ever seeds a
    term_consistency check. This is the single choke point for BOTH the event
    and ADR pair generators (DP10 — one guard, every source).
    """
    t = (term or "").strip()
    if not t or not _TERM_LETTER_RE.search(t):
        return False
    return not _MEASUREMENT_TERM_RE.match(t)


def _is_safe_autofix_token(value: str, *, max_len: int = 40) -> bool:
    """True when ``value`` is safe to use as an automatic term→replacement
    rewrite: short, carries a letter, and shows no value-like signal (``=``,
    ``%``, grouped/long digit runs). Verbose or numeric values return False so
    the autofix never substitutes a sentence/number for a token."""
    v = (value or "").strip()
    if not v or len(v) > max_len:
        return False
    if not _TERM_LETTER_RE.search(v):
        return False
    if _VALUE_LIKE_RE.search(v):
        return False
    return True


def _candidate_terms(value: str) -> set[str]:
    """Extract distinctive terms from a decision choice / new_choice.

    Heuristic: token + bigrams, lowercase, len>=3, not in stopword list.
    Returns a small set; the runner does the grep so a low-precision /
    high-recall extraction is fine — the Critic catches false positives.

    Parenthetical asides are STRIPPED before tokenizing. A decision choice's
    subject lives OUTSIDE the parens ("Playwright-ea MCP browser"); the
    parenthetical holds incidental config detail ("(file:// URL, 1440px
    viewport, headless=false)"). Tokenizing the whole string turned every
    config noun (viewport, headless, url) into a drift term — and because
    term_consistency findings are ``load_bearing`` they SHORT-CIRCUIT the
    Critic that was meant to filter such false positives, producing an
    UNRECOVERABLE block (task-20260623-021230: "viewport" from a verbose
    browser-tooling supersession matched ``<meta name="viewport">`` in an
    unrelated HTML deck and looped to NEEDS_USER). Real drift subjects sit
    outside parens, so this preserves true positives (the jinja2/11ty case
    has no parenthetical).
    """
    if not value or not isinstance(value, str):
        return set()
    cleaned = _PARENTHETICAL_RE.sub(" ", value)
    raw = _TERM_SPLIT_RE.split(cleaned.strip())
    toks = [t for t in raw if t and len(t) >= 3 and t.lower() not in _STOP_TERMS]
    if not toks:
        return set()
    # SINGLE-token label → that token IS the drift subject ("jinja2").
    if len(toks) == 1:
        return {toks[0].lower()}
    # MULTI-word label → the drift subject is the WHOLE PHRASE, not its
    # constituent words. Tokenizing a prose decision label ("Clean resource
    # REST") into per-word drift terms turned ordinary English (clean,
    # resource, rest) into load-bearing checks that ANY domain deliverable
    # must trip — an UNRECOVERABLE block (a real api-paradigm gate: a REST API
    # spec cannot avoid the words "rest"/"resource"/"clean", so every retry
    # re-failed identically to the cap). Emit the full phrase as ONE literal
    # term; additionally keep individually DISTINCTIVE tokens (digits / -_. /
    # camelCase — "11ty", "next.js") which are real drift subjects on their
    # own. Generic dictionary words never become standalone terms.
    out: set[str] = {" ".join(t.lower() for t in toks)}
    out.update(t.lower() for t in toks if _is_distinctive_token(t))
    return out


def _superseded_terms_from_events(events: Iterable[dict]) -> list[tuple[str, str, str]]:
    """Walk supersedes events. Return [(superseded_term, replacement_term, origin), ...].

    Replacement comes from the supersedes body's ``new_choice``; the
    superseded term comes from the target event's ``choice``. Both are
    tokenized into candidate terms; the runner greps each.
    """
    by_id: dict[str, dict] = {}
    for e in events:
        eid = e.get("id")
        if eid:
            by_id[eid] = e
    pairs: list[tuple[str, str, str]] = []
    for e in events:
        if e.get("event_type") != "supersedes":
            continue
        body = e.get("body") or {}
        target_id = body.get("target_event_id") or ""
        new_choice = (body.get("new_choice") or "").strip()
        target = by_id.get(target_id)
        if not target:
            continue
        target_body = target.get("body") or {}
        prior_choice = (target_body.get("choice") or "").strip()
        if not prior_choice or not new_choice:
            continue
        origin = (
            f"event:{target_id[:8]} → superseded by {e.get('id', '?')[:8]} "
            f"({prior_choice!r} → {new_choice!r})"
        )
        for prior_term in _candidate_terms(prior_choice):
            # Skip if the term also appears in new_choice (overlap → not drift).
            new_terms = _candidate_terms(new_choice)
            if prior_term in new_terms:
                continue
            # P1 root guard — a bare-number "term" (e.g. "823" split out of
            # "10,823") is a value, not terminology drift; never seed a check.
            if not _is_term_candidate(prior_term):
                continue
            pairs.append((prior_term, new_choice, origin))
    return pairs


def _superseded_terms_from_adrs(adrs: Iterable[dict]) -> list[tuple[str, str, str]]:
    """ADR-driven drift terms.

    M1 ADRs encode the WINNING choice (selected_label / selected_option_id).
    The LOSING choices live in adr['options'] — every non-selected option
    is a potential drift term. Same shape return as the events helper.
    """
    pairs: list[tuple[str, str, str]] = []
    for adr in adrs or []:
        if not isinstance(adr, dict):
            continue
        chosen = (adr.get("selected_label") or adr.get("selected_option_id") or "").strip()
        if not chosen:
            continue
        options = adr.get("options") or []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            opt_label = (opt.get("label") or opt.get("id") or "").strip()
            if not opt_label or opt_label == chosen:
                continue
            origin = (
                f"ADR gate {adr.get('gate_id', '?')} — chose {chosen!r}, "
                f"rejected {opt_label!r}"
            )
            for prior_term in _candidate_terms(opt_label):
                new_terms = _candidate_terms(chosen)
                if prior_term in new_terms:
                    continue
                # P1 root guard — drop bare-number "terms" (see events helper).
                if not _is_term_candidate(prior_term):
                    continue
                pairs.append((prior_term, chosen, origin))
    return pairs


def _deliverable_paths(subtasks: Iterable[Any]) -> list[Path]:
    """Collect existing on-disk deliverables from subtask outputs / targets."""
    seen: set[str] = set()
    out: list[Path] = []
    for st in subtasks or []:
        for attr in ("outputs", "target_paths"):
            raw = getattr(st, attr, None) or []
            for p in raw:
                if not isinstance(p, str) or not p.strip():
                    continue
                pp = Path(p.strip())
                # Outputs are often repo-relative; try a few resolutions.
                candidates = [pp]
                if not pp.is_absolute():
                    project = getattr(st, "_project_path", None) or _guess_project(st)
                    if project:
                        candidates.append(Path(project) / pp)
                for c in candidates:
                    s = str(c)
                    if s in seen:
                        continue
                    if c.exists() and c.is_file():
                        seen.add(s)
                        out.append(c)
                        break
    return out


def _guess_project(subtask: Any) -> str:
    """Best-effort project root inference from declared target paths."""
    for attr in ("target_paths", "outputs"):
        raw = getattr(subtask, attr, None) or []
        for p in raw:
            if isinstance(p, str) and p.startswith("/"):
                # Walk up until we find a git root or just take the dirname.
                pp = Path(p)
                for parent in [pp.parent, *pp.parents]:
                    if (parent / ".git").exists():
                        return str(parent)
        # No absolute paths — give up; the caller will use cwd.
    return ""


# Cap each brain body so a runaway deliverable can't blow the grep memory /
# latency budget. 200k chars ≈ 50k tokens — far above any real deliverable.
_BRAIN_BODY_CAP = 200_000


def _brain_deliverable_sources(task: Any, phase: Any) -> list[dict]:
    """Fetch the current (non-superseded) brain-artifact bodies for every
    subtask in ``phase``.

    Deliberation / research tasks write their deliverables to the brain
    (``artifact_write`` → ``brain://<id>``), NOT to ``subtask.outputs`` files
    on disk — so :func:`_deliverable_paths` returns nothing for them and the
    deterministic checks (term_consistency in particular) were structurally
    blind to the actual deliverable. This closes that gap: every deterministic
    check that greps deliverable text can now also see brain bodies.

    Returns ``[{"locus": "brain://<id>", "text": <body>, "subtask_id": sid}]``.
    Degrades to ``[]`` on any import / DB failure so unit tests without a DB
    (and a brain outage in prod) never abort the check chain.
    """
    task_id = getattr(task, "id", "") or ""
    if not task_id:
        return []
    try:
        from okuro.sense.artifacts import artifact_list, artifact_get
    except Exception:
        return []
    out: list[dict] = []
    for st in (getattr(phase, "subtasks", []) or []):
        sid = str(getattr(st, "id", "") or "")
        if not sid:
            continue
        try:
            rows = artifact_list(
                task_id=task_id, subtask_id=sid, limit=20,
                include_superseded=False,
            )
        except Exception:
            continue
        for r in rows or []:
            aid = (r or {}).get("id") or ""
            if not aid:
                continue
            try:
                full = artifact_get(aid, include_body=True) or {}
            except Exception:
                continue
            body = full.get("body") or ""
            if not body:
                continue
            out.append({
                "locus": f"brain://{aid}",
                "text": body[:_BRAIN_BODY_CAP],
                "subtask_id": sid,
                # WP-G — carried so each check can declare WHICH sources it
                # greps. `kind` alone is unreliable: measured on one live task,
                # two AC-evidence artifacts were written as kind="evidence" and
                # kind="report" respectively, both titled "… — AC evidence".
                # So the title is checked too.
                "kind": (r or {}).get("kind") or "",
                "title": (r or {}).get("title") or "",
            })
    return out


# WP-G — source-scope policy for the text-scanning checks.
#
# THE BUG THIS EXISTS FOR, observed live: a report was corrected to drop a
# superseded decision term, and the AC-EVIDENCE artifact then NAMED that term
# in order to prove it was gone. term_consistency counted the mentions and
# failed the round — round 1 "2 places", round 2 "3 places", so the identical-
# findings fingerprint guard did not fire either and the loop had no exit.
#
# Proving a term's absence requires naming it. An evidence artifact documents
# the deliverable; it is not the deliverable, and must not be graded as one.
#
# Scope is declared per check rather than hardcoded in one, because the same
# blindness applies to any future text-scanning check:
#   term_consistency   deliverable only  — evidence legitimately quotes terms
#   numeric_consistency all              — evidence carries the numbers ledger
#   secret_name        all               — a leaked secret is a leak anywhere


def _is_evidence_source(src: dict) -> bool:
    """True when this brain source documents the deliverable rather than IS it."""
    kind = str((src or {}).get("kind") or "").lower()
    title = str((src or {}).get("title") or "").lower()
    return kind == "evidence" or "ac evidence" in title or "ac-evidence" in title


def _deliverable_sources_only(sources: Optional[list[dict]]) -> list[dict]:
    """Drop evidence artifacts. See the scope policy note above."""
    return [s for s in (sources or []) if not _is_evidence_source(s)]


# ---------------------------------------------------------------------------
# Per-kind check generators
# ---------------------------------------------------------------------------


def _gen_numeric_consistency(
    deliverables: list[Path],
    brain_sources: Optional[list[dict]] = None,
) -> list[Check]:
    """One ``numeric_consistency`` check over all deliverable text (Phase B).

    The runner recomputes any ```numbers ledger deterministically. No ledger
    present → no findings → PASS, so this is harmless on non-numeric tasks and
    only ever bites when a declared total fails to reconcile. ``load_bearing``
    so a numeric defect short-circuits the LLM critic+scorer (pipeline.py) —
    a wrong total FAILs in <50ms with 0 tokens instead of looping the Critic.
    """
    brain_sources = brain_sources or []
    if not deliverables and not brain_sources:
        return []
    return [Check(
        kind="numeric_consistency",
        params={
            "files": [str(p) for p in deliverables],
            "brain_sources": brain_sources,
        },
        origin="numbers-ledger reconciliation",
        severity="load_bearing",
    )]


def _gen_term_consistency(
    superseded_pairs: list[tuple[str, str, str]],
    deliverables: list[Path],
    brain_sources: Optional[list[dict]] = None,
) -> list[Check]:
    """One Check per superseded term — runner greps for the term across BOTH
    on-disk deliverables and brain-artifact bodies.

    ``brain_sources`` (from :func:`_brain_deliverable_sources`) lets the check
    see deliberation/research deliverables that live only in the brain, not on
    disk — the dominant deliverable type for decision-laden tasks. A check is
    emitted when there is at least one source of EITHER kind.
    """
    checks: list[Check] = []
    # WP-G — evidence artifacts quote a superseded term in order to prove it
    # was removed. Grading them against it makes the finding unfixable.
    brain_sources = _deliverable_sources_only(brain_sources)
    if not superseded_pairs or (not deliverables and not brain_sources):
        return checks
    for term, replacement, origin in superseded_pairs:
        checks.append(Check(
            kind="term_consistency",
            params={
                "term": term,
                "replacement": replacement,
                "files": [str(p) for p in deliverables],
                "brain_sources": brain_sources,
            },
            origin=origin,
            severity="load_bearing",
        ))
    return checks


def _resolve_tasks_dir(explicit: Optional[Path | str] = None) -> Path:
    """Locate the orchestrator tasks directory without importing the
    engine's Config module (which would cycle through the bridge).

    Resolution order (WP7 config-threading): an ``explicit`` value passed
    by the engine (via ``config.tasks_dir``) ALWAYS wins, so a non-default
    ``--config`` run resolves file checks against the SAME tasks_dir the
    engine dispatched under. Falls back to ``OKURO_TASKS_DIR`` (tests),
    then the canonical ~/.okuro/orchestrator/tasks path the launcher uses.
    """
    if explicit:
        return Path(explicit)
    override = os.environ.get("OKURO_TASKS_DIR")
    if override:
        return Path(override)
    return okuro_home() / "orchestrator" / "tasks"


def _gen_file_exists(subtasks: Iterable[Any], task: Any = None,
                     tasks_dir: Optional[Path | str] = None) -> list[Check]:
    # P0-3 — task context lets the runner try multiple path resolutions
    # (task artifacts dir, subtask-id-prefixed legacy save_artifact name,
    # brain artifact rows, project_path-relative) before declaring the
    # output missing. Without it, `outputs=["foo.md"]` resolves against
    # the engine subprocess cwd (~/.okuro/orchestrator), where nothing
    # ever lands — so every legitimate deliverable failed the gate.
    #
    # WP7 — ``tasks_dir`` is threaded from the engine's config so file
    # checks resolve against the SAME tasks_dir the engine dispatched
    # under (not the default) when a non-default --config is in play.
    task_id = getattr(task, "id", "") or ""
    project_path = getattr(task, "project_path", "") or ""
    tasks_dir = str(_resolve_tasks_dir(tasks_dir))
    checks: list[Check] = []
    for st in subtasks or []:
        subtask_id = getattr(st, "id", "?")
        artifact_name = (getattr(st, "artifact_name", "") or "").strip()
        for attr in ("outputs", "target_paths"):
            raw = getattr(st, attr, None) or []
            for p in raw:
                if not isinstance(p, str) or not p.strip():
                    continue
                checks.append(Check(
                    kind="file_exists",
                    params={
                        "path": p.strip(),
                        "subtask_id": subtask_id,
                        "task_id": task_id,
                        "project_path": project_path,
                        "tasks_dir": tasks_dir,
                        "artifact_name": artifact_name,
                    },
                    origin=f"subtask {subtask_id} declared {attr}",
                    severity="load_bearing",
                ))
    return checks


_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HTML_HREF_RE = re.compile(r'(?:href|src)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _gen_link_integrity(deliverables: list[Path]) -> list[Check]:
    checks: list[Check] = []
    for f in deliverables:
        suffix = f.suffix.lower()
        if suffix not in {".md", ".html", ".htm"}:
            continue
        checks.append(Check(
            kind="link_integrity",
            params={"file": str(f)},
            origin=f"markdown/html deliverable {f.name}",
            severity="cosmetic",  # broken links are usually not load-bearing
        ))
    return checks


def _gen_schema_assert(subtasks: Iterable[Any]) -> list[Check]:
    """Output extension consistency — declared vs on-disk."""
    checks: list[Check] = []
    for st in subtasks or []:
        raw = getattr(st, "outputs", None) or []
        for p in raw:
            if not isinstance(p, str) or not p.strip():
                continue
            declared = Path(p.strip()).suffix.lower()
            if not declared:
                continue
            checks.append(Check(
                kind="schema_assert",
                params={"path": p.strip(), "expected_suffix": declared,
                        "subtask_id": getattr(st, "id", "?")},
                origin=f"subtask {getattr(st, 'id', '?')} declared output {p}",
                severity="cosmetic",
            ))
    return checks


_KEYRING_RE = re.compile(r"keyring_get\s*\(\s*[\"']([^\"']+)[\"']")


# ---------------------------------------------------------------------------
# AC evidence shape — Theme B
#
# The Critic enforces §C11's "captured stdout / exit-code per AC" rule but
# observed runs (2026-05-29, phases 4.2/4.3/5.1) showed
# subagents emitting prose-only or grep-only evidence. The Critic FAILs on
# format alone, findings_count oscillates 1→8→4, retry loop never resolves
# even when the underlying work is correct.
#
# This check pre-screens artifact bodies BEFORE the LLM critic sees them.
# Missing-evidence findings are tagged ``severity="infra_error"`` so they
# DO NOT count toward ``load_bearing_failures`` in :func:`summarize` (which
# only counts ``load_bearing``). The engine still surfaces them in the
# verdict + retry brief so the subagent fixes its evidence shape, but the
# deterministic gate does not short-circuit on format defects.
# ---------------------------------------------------------------------------

# Match "## AC1", "### AC#2", "## AC 3 —", "AC#4:", etc. Case-insensitive.
# Anchor on either a heading marker or a line-start AC# pattern.
_AC_ANCHOR_RE = re.compile(
    r"(?m)^(?:#{1,6}\s+)?AC\s*#?\s*(\d+)\b",
    re.IGNORECASE,
)
# Triple-backtick fenced block opener with `bash`, `sh`, `console`, `shell` lang.
_BASH_FENCE_RE = re.compile(
    r"```\s*(?:bash|sh|shell|console)\b[^\n]*\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
# Heuristic: a $-prompt-prefixed command line inside the bash block.
_DOLLAR_PROMPT_RE = re.compile(r"(?m)^\s*\$\s+\S+")


def _check_ac_evidence_shape(
    artifact_text: str,
    acs: list[str] | list[int],
    *,
    window: int = 800,
) -> list[dict]:
    r"""Scan ``artifact_text`` for each AC's evidentiary bash block.

    For each AC index ``i`` (1-based) in ``acs``:
      1. Locate the AC anchor in ``artifact_text`` (regex matching ``## AC1``,
         ``### AC#2``, ``AC 3``, etc.).
      2. Read the next ``window`` chars.
      3. Look for at least one ```bash``` (or ``sh`` / ``shell`` /
         ``console``) fenced block.
      4. Inside that block, require at least one ``$ <cmd>`` line OR at
         least one non-blank line that LOOKS like captured stdout (the
         block has at least 2 non-blank lines — command + output).

    Each missing AC yields one finding dict with severity-marker fields the
    runner wraps into a CheckResult. Returns an empty list on full compliance.

    Tolerated shapes (do NOT flag):
      - The block is in a sibling section but the AC anchor still appears.
      - The block uses ``bash``, ``sh``, ``shell``, or ``console`` as the
        language tag — all four are accepted.
      - The block contains no ``$ `` prefix but has >=2 non-blank lines
        (command + output, no prompt — common shape from copy-paste).

    Flagged shapes (DO flag):
      - No AC anchor found at all.
      - AC anchor found but no bash fence within the next ``window`` chars.
      - Bash fence found but it has fewer than 2 non-blank lines (empty
        or single-line block — no real evidence).
    """
    findings: list[dict] = []
    if not artifact_text or not acs:
        return findings

    # Build a map of AC# → first occurrence offset (so we can take the
    # next ``window`` chars). Multiple matches for the same AC# fall back
    # to the first — agents typically open with the AC heading and may
    # cite it again later in prose.
    anchors: dict[int, int] = {}
    for m in _AC_ANCHOR_RE.finditer(artifact_text):
        try:
            n = int(m.group(1))
        except (TypeError, ValueError):
            continue
        anchors.setdefault(n, m.end())

    # Iterate the planned ACs (1-based). The check is per-declared-AC, so a
    # body that quotes ACs the planner did not enumerate is fine.
    for i in range(1, len(acs) + 1):
        anchor_end = anchors.get(i)
        ac_text = acs[i - 1] if isinstance(acs[i - 1], str) else ""
        if anchor_end is None:
            findings.append({
                "ac_number": i,
                "ac_text": ac_text[:200],
                "evidence": f"No `## AC{i}` (or AC#{i}) anchor found in artifact body.",
                "suggested_fix": (
                    f"Add a `## AC{i}` heading followed by a ```bash``` block "
                    f"containing the actual command, its stdout (cap 500 "
                    f"chars), and exit code. Prose-only claims are forbidden."
                ),
            })
            continue
        window_text = artifact_text[anchor_end: anchor_end + window]
        # Stop at the next AC anchor — don't bleed evidence from a later AC.
        next_anchor = _AC_ANCHOR_RE.search(window_text)
        if next_anchor:
            window_text = window_text[: next_anchor.start()]
        fence_match = _BASH_FENCE_RE.search(window_text)
        if not fence_match:
            findings.append({
                "ac_number": i,
                "ac_text": ac_text[:200],
                "evidence": f"AC#{i} anchor present but no ```bash``` block in next {window} chars.",
                "suggested_fix": (
                    f"Add a ```bash``` (or ```sh```) fenced block under the "
                    f"AC{i} heading with: (1) the command you ran, "
                    f"(2) its stdout, (3) its exit code. The reviewer scans "
                    f"for this exact shape."
                ),
            })
            continue
        block_body = fence_match.group(1) or ""
        non_blank = [ln for ln in block_body.splitlines() if ln.strip()]
        has_prompt = bool(_DOLLAR_PROMPT_RE.search(block_body))
        if len(non_blank) < 2 and not has_prompt:
            findings.append({
                "ac_number": i,
                "ac_text": ac_text[:200],
                "evidence": (
                    f"AC#{i} has a bash block but it is empty or single-line "
                    f"({len(non_blank)} non-blank lines, no `$ ` prompt) — "
                    f"no recognizable command+stdout pair."
                ),
                "suggested_fix": (
                    f"Inside the AC{i} bash block, include: (line 1) `$ "
                    f"<command>`; (line 2+) the captured stdout from that "
                    f"command; (final line) `exit: <code>`."
                ),
            })
    return findings


def _gen_brain_disk_divergence(
    subtasks: Iterable[Any], task: Any = None,
    tasks_dir: Optional[Path | str] = None,
) -> list[Check]:
    """ROCK-SOLID v5 P4.11 — did the disk file move after the artifact was written?

    A subtask can declare an output, write it to disk, ALSO record it as a
    brain artifact, and then keep editing the file. The reviewer reads the
    artifact body — so from that point it grades a snapshot that no longer
    matches what shipped, and it does so invisibly: nothing about the review
    says which of the two it read.

    Autofix makes it worse in one direction: it mutates the BRAIN body by
    policy, never the disk file, so an autofixed artifact and its disk twin
    diverge further with each round.

    Advisory, not a gate. A file legitimately newer than its artifact is
    common (a formatter ran, a build touched it); the finding says the two
    disagree and which is newer, and lets the critic weigh it.
    """
    task_id = getattr(task, "id", "") or ""
    project_path = getattr(task, "project_path", "") or ""
    tasks_dir_s = str(_resolve_tasks_dir(tasks_dir))
    checks: list[Check] = []
    for st in subtasks or []:
        subtask_id = getattr(st, "id", "?")
        for attr in ("outputs", "target_paths"):
            for raw in (getattr(st, attr, None) or []):
                if not isinstance(raw, str) or not raw.strip():
                    continue
                checks.append(Check(
                    kind="brain_disk_divergence",
                    params={
                        "path": raw.strip(),
                        "task_id": task_id,
                        "subtask_id": subtask_id,
                        "project_path": project_path,
                        "tasks_dir": tasks_dir_s,
                        "artifact_name": (getattr(st, "artifact_name", "") or "").strip(),
                    },
                    origin=f"declared output {raw.strip()} on subtask {subtask_id}",
                    # ADVISORY. A file legitimately newer than its artifact is
                    # common (a formatter, a build). Load-bearing would fail
                    # the phase on it, which is the wrong end of the trade.
                    severity="cosmetic",
                ))
    return checks


def _gen_ac_evidence_shape(subtasks: Iterable[Any], task: Any = None) -> list[Check]:
    """One Check per subtask with declared acceptance_criteria.

    The runner fetches the subtask's artifact_write body lazily (brain
    rows for ``(task_id, subtask_id)``) and pipes it through
    :func:`_check_ac_evidence_shape`. Missing brain row → no finding
    (file_exists already covers that case at load-bearing severity).
    """
    task_id = getattr(task, "id", "") or ""
    checks: list[Check] = []
    for st in subtasks or []:
        subtask_id = getattr(st, "id", "?")
        acs = list(getattr(st, "acceptance_criteria", None) or [])
        if not acs:
            continue
        checks.append(Check(
            kind="ac_evidence_shape",
            params={
                "subtask_id": subtask_id,
                "task_id": task_id,
                "acceptance_criteria": acs,
            },
            origin=f"subtask {subtask_id} declared {len(acs)} acceptance_criteria",
            severity="infra_error",  # excluded from load_bearing_failures
        ))
    return checks


def _gen_secret_name(
    deliverables: list[Path],
    known_names: set[str],
    brain_sources: Optional[list[dict]] = None,
) -> list[Check]:
    """One Check per on-disk deliverable, plus one over any inline sources.

    ``brain_sources`` mirrors ``_gen_term_consistency`` /
    ``_gen_numeric_consistency`` so all three text-scanning checks accept the
    same ``{locus, text, subtask_id}`` shape. Without it this check was blind
    to brain-only deliverables, and unusable by any caller holding text that
    is not yet on disk.
    """
    if not known_names:
        return []
    checks = [Check(
        kind="secret_name",
        params={"file": str(f), "known_names": sorted(known_names)},
        origin="cross-deliverable secret-name consistency",
        severity="load_bearing",
    ) for f in deliverables if f.suffix.lower() in {".md", ".py", ".sh"}]
    if brain_sources:
        checks.append(Check(
            kind="secret_name",
            params={
                "known_names": sorted(known_names),
                "brain_sources": brain_sources,
            },
            origin="cross-deliverable secret-name consistency (inline sources)",
            severity="load_bearing",
        ))
    return checks


def _known_secret_names(adrs: Iterable[dict], events: Iterable[dict]) -> set[str]:
    out: set[str] = set()
    for adr in adrs or []:
        sel = (adr.get("selected_label") or "")
        for m in _KEYRING_RE.finditer(sel or ""):
            out.add(m.group(1))
    for e in events or []:
        if e.get("event_type") not in {"decision", "contract"}:
            continue
        body = e.get("body") or {}
        blob = " ".join(str(v) for v in body.values() if isinstance(v, str))
        for m in _KEYRING_RE.finditer(blob):
            out.add(m.group(1))
    return out


# ---------------------------------------------------------------------------
# generate_checks — top-level entry
# ---------------------------------------------------------------------------


def generate_checks(
    *,
    task: Any,
    phase: Any,
    events: Optional[list[dict]] = None,
    adrs: Optional[list[dict]] = None,
    tasks_dir: Optional[Path | str] = None,
    profile: Any = None,
) -> list[Check]:
    """Read plan + ADRs + event log → emit deterministic Checks.

    The caller hands in already-loaded ``events`` (from
    :func:`okuro.sense.task_events.list_events`) and ``adrs`` (from
    ``task.adrs``). Both default to empty when omitted so this stays
    callable from unit tests without a DB.

    ``tasks_dir`` (WP7) is the engine's resolved ``config.tasks_dir``,
    threaded into ``file_exists`` checks so they resolve against the same
    tasks directory the engine dispatched under. ``None`` keeps the
    env/default fallback for callers that don't pass it.

    ``profile`` (kind-aware review) is a
    :class:`okuro.orchestrator.reviewer.profiles.ReviewProfile`. When given,
    only checks whose kind is in ``profile.det_checks`` survive — so a
    ``plan``/``report`` artifact is not held to the executable
    ``ac_evidence_shape`` / ``secret_name`` bar that only code can satisfy.
    ``None`` keeps the full legacy check set for every caller that doesn't
    pass it.
    """
    events = events or []
    adrs = adrs or []
    subtasks = list(getattr(phase, "subtasks", []) or [])

    deliverables = _deliverable_paths(subtasks)
    # Brain-only deliverables (deliberation / research tasks write to the
    # brain, not subtask.outputs) — so the term-consistency / locked-decision
    # check can see them. Best-effort; degrades to [] without a DB.
    brain_sources = _brain_deliverable_sources(task, phase)
    sup_pairs = _superseded_terms_from_events(events) + _superseded_terms_from_adrs(adrs)
    known_secrets = _known_secret_names(adrs, events)

    checks: list[Check] = []
    checks.extend(_gen_term_consistency(sup_pairs, deliverables, brain_sources))
    checks.extend(_gen_numeric_consistency(deliverables, brain_sources))
    checks.extend(_gen_file_exists(subtasks, task=task, tasks_dir=tasks_dir))
    checks.extend(_gen_link_integrity(deliverables))
    checks.extend(_gen_schema_assert(subtasks))
    checks.extend(_gen_secret_name(deliverables, known_secrets, brain_sources))
    checks.extend(_gen_ac_evidence_shape(subtasks, task=task))
    checks.extend(_gen_brain_disk_divergence(subtasks, task=task, tasks_dir=tasks_dir))
    if profile is not None:
        allowed = getattr(profile, "det_checks", None)
        if allowed is not None:
            checks = [c for c in checks if c.kind in allowed]
    return checks


# ---------------------------------------------------------------------------
# Check runners
# ---------------------------------------------------------------------------


_WORD_BOUNDARY_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _term_regex(term: str) -> re.Pattern[str]:
    """Compile a case-insensitive whole-word regex for a term.

    Cached because the same term reappears across many deliverable files.
    Special handling: terms containing non-word chars (e.g. "11ty +
    nunjucks") fall back to literal substring matching.
    """
    if term in _WORD_BOUNDARY_RE_CACHE:
        return _WORD_BOUNDARY_RE_CACHE[term]
    if re.fullmatch(r"[A-Za-z0-9_]+", term):
        pat = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    else:
        pat = re.compile(re.escape(term), re.IGNORECASE)
    _WORD_BOUNDARY_RE_CACHE[term] = pat
    return pat


# P2 (audit #4) — a line that DESCRIBES the rejection ("we rejected 11ty in
# favor of nunjucks", "Jinja2 deprecated, replaced by 11ty") legitimately names
# the rejected term; flagging it is a false positive, and because such mentions
# live in sibling/discussion bodies the retrying author cannot edit, the FAIL is
# UNRECOVERABLE (loops to CAP). The cure: skip a hit when the matched line
# carries an explicit rejection-cue phrase — i.e. the term is being TALKED ABOUT,
# not asserted as a current choice. Verified against the headline drift corpus:
# all five genuine jinja2 drift lines carry NO cue, so true positives still block.
_REJECTION_CUE_RE = re.compile(
    r"\b(?:reject(?:ed|s|ing)?|deprecat(?:ed|es|ing)?|supersed(?:ed|es|ing)?|"
    r"abandon(?:ed|ing)?|in\s+favou?r\s+of|instead\s+of|rather\s+than|"
    r"replaced\s+by|no\s+longer|not\s+using|(?:mov|migrat)(?:ed|ing)\s+away\s+from|"
    r"phased\s+out|dropped\s+in\s+favou?r)\b",
    re.IGNORECASE,
)


# --- Context: WHERE a term sits decides whether it is prose at all -----------
# The rejection-cue guard above matches PHRASING. It cannot see CONTEXT, so a
# term inside a product URL, a code span or a shell-evidence line reads exactly
# like a live assertion of the rejected choice. Live on task-20260724-110216:
#
#   | RAM x2 | qty cut 3->2 | example.com/…/acme-64gb-ddr5-4800mts-ecc-reg-… |
#   $ echo "Case airflow: front intake only, rear 80mm exhaust, no top/side vents"
#
# A URL slug is a vendor's SKU string and a shell line is executed evidence —
# neither is the author choosing terminology, and neither is editable without
# breaking the citation, which makes the FAIL unrecoverable (it loops to CAP).
#
# So mask the non-prose spans and match only what is left. Masking preserves
# LENGTH (spans become spaces) so offsets stay valid for the autofix, which
# must skip the same spans or it would rewrite the inside of a URL.
#
# Verified against the headline drift corpus: all five genuine jinja2 drift lines
# carry the term in prose — the code spans on those lines hold unrelated paths
# (`site_publish.py`, `*.example.io`) — so true positives still block.
_NON_PROSE_SPAN_RES = (
    re.compile(r"`[^`\n]*`"),                             # inline code span
    re.compile(r"\]\([^)\n]*\)"),                         # md link destination
    re.compile(r"<[A-Za-z][\w+.-]*://[^>\s]*>"),          # autolink
    re.compile(r"\b[A-Za-z][\w+.-]*://\S+"),              # scheme URL
    re.compile(r"\bwww\.\S+"),                            # bare www URL
    re.compile(r"\b[\w-]+(?:\.[A-Za-z][\w-]+)+/\S*"),     # bare host + path
    re.compile(                                           # html url attribute
        r"\b(?:href|src|action|data-[\w-]+)\s*=\s*(?:\"[^\"\n]*\"|'[^'\n]*')",
        re.IGNORECASE,
    ),
)

# A shell/REPL evidence line is a transcript, not an assertion.
_EVIDENCE_LINE_RE = re.compile(r"^\s*(?:\$\s|>>>\s|#\s*\$\s)")


def _prose_only(line: str) -> str:
    """Return ``line`` with every non-prose span blanked, same length.

    An evidence/transcript line collapses to blanks entirely. Callers match
    against the result but keep the ORIGINAL line for display, so a finding
    still quotes what the author actually wrote.
    """
    if not line:
        return line
    if _EVIDENCE_LINE_RE.match(line):
        return " " * len(line)
    out = line
    for rx in _NON_PROSE_SPAN_RES:
        out = rx.sub(lambda m: " " * len(m.group(0)), out)
    return out


def _replacement_markers(replacement: str) -> list[re.Pattern[str]]:
    """Regexes for the terms that identify the NEW choice.

    Only distinctive tokens (and the full phrase) qualify — a generic word out
    of a prose new_choice would suppress unrelated lines wholesale.
    """
    out: list[re.Pattern[str]] = []
    for t in sorted(_candidate_terms(replacement or "")):
        if " " in t or _is_distinctive_token(t):
            out.append(_term_regex(t))
    return out


def _line_documents_change(
    line: str,
    replacement_markers: Iterable[re.Pattern[str]] = (),
) -> bool:
    """True when the line is ABOUT the supersession rather than drifting into it.

    Two independent signals:

    * an explicit rejection cue (":data:`_REJECTION_CUE_RE`") — phrasing;
    * the NEW term appears on the SAME line as the old one — structure. A
      before/after table row or a substitution note names the old value on
      purpose; that is documentation, and demanding its removal destroys the
      record of the change. Worked example: ``| PSU | Northwind NP-750 |
      Northwind NP-850 | same 850W/ATX3.1 |``.

    Deliberately NOT a signal: an arrow (``->`` / ``→``). Two of the five
    genuine drift lines use arrows for DATA FLOW ("render Jinja2 templates
    → static HTML"), so an arrow cue would silence real drift. Likewise bare
    "replaces" — one line in that corpus uses it about an unrelated subject.
    """
    if _REJECTION_CUE_RE.search(line):
        return True
    return any(p.search(line) for p in replacement_markers)


def _sub_in_prose(line: str, subs: Iterable[tuple[re.Pattern[str], str]]) -> str:
    """Apply ``(pattern, replacement)`` rewrites to ``line``, skipping matches
    that land inside a non-prose span.

    Matches are collected against the ORIGINAL line and applied left to right
    in one pass, so earlier rewrites never shift the offsets of later ones.
    """
    masked = _prose_only(line)
    spans: list[tuple[int, int, str]] = []
    for pat, repl in subs:
        for m in pat.finditer(line):
            if masked[m.start():m.end()] != m.group(0):
                continue  # inside a URL / code span / evidence transcript
            spans.append((m.start(), m.end(), repl))
    if not spans:
        return line
    spans.sort(key=lambda s: (s[0], s[1]))
    out: list[str] = []
    last = 0
    for start, end, repl in spans:
        if start < last:
            continue  # overlapping match already consumed
        out.append(line[last:start])
        out.append(repl)
        last = end
    out.append(line[last:])
    return "".join(out)


# A superseded DECISION is decomposed into candidate terms, but its replacement
# is NOT — `_superseded_terms_from_events` pairs every extracted old term with
# the whole ``new_choice`` blob. So "Replace '850w' with <a 300-character RAM
# and cooling specification>" is what the data actually says, and it is
# nonsense as an instruction. Only claim a term-for-term rename when the
# replacement really is one term; otherwise describe the decision and point at
# the places, which is both true and answerable.
_MAX_INLINE_REPLACEMENT = 60


def _term_fix_text(term: str, replacement: str, origin: str, count: int) -> str:
    rep = (replacement or "").strip()
    where = "1 place" if count == 1 else f"{count} places"
    if rep and len(rep) <= _MAX_INLINE_REPLACEMENT and len(_candidate_terms(rep)) == 1:
        return f"Replace {term!r} with {rep!r} in {where} ({origin})"
    short = rep[:120] + ("…" if len(rep) > 120 else "")
    return (
        f"{term!r} comes from a superseded choice but still appears in {where}. "
        f"The current decision is: {short}. Update or remove those mentions."
    )


def _run_term_consistency(check: Check) -> CheckResult:
    term: str = check.params["term"]
    replacement: str = check.params["replacement"]
    files: list[str] = check.params.get("files") or []
    brain_sources: list[dict] = check.params.get("brain_sources") or []
    pat = _term_regex(term)
    markers = _replacement_markers(replacement)
    findings: list[dict] = []

    # One finding per TERM, not per matching line. Emitting per-occurrence made
    # the finding count a property of how often a word appears rather than of
    # how many things are wrong: one superseded decision produced three terms,
    # which matched on five lines, which surfaced to the user as five separate
    # "MUST FIX" cards carrying identical bodies. The occurrences are all kept
    # on the finding, so nothing is lost and autofix still has every locus.
    hits: list[dict] = []

    def _scan(locus: str, text: str, subtask_id: Optional[str] = None) -> None:
        for i, line in enumerate(text.splitlines(), 1):
            # Match PROSE only — a hit inside a URL slug, a code span or a
            # shell-evidence transcript is not the author asserting a choice.
            if pat.search(_prose_only(line)):
                # Skip lines that DESCRIBE the change rather than drift into
                # using the term as a live choice (false-positive guard):
                # rejection phrasing, or the new term on the same line.
                if _line_documents_change(line, markers):
                    continue
                hits.append({
                    "file": locus,
                    "line": i,
                    # P3 (#10) — carry the owning subtask id (brain sources
                    # know it) so rerun_subtasks_from_verdict (pipeline.py:792)
                    # resets the subtask whose body actually drifted, instead of
                    # the last-done guess (a brain:// locus never overlaps a
                    # filesystem output path, so file-routing always missed it).
                    "subtask_id": subtask_id,
                    "evidence": line.strip()[:200],
                })

    for fp in files:
        try:
            text = Path(fp).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return CheckResult(check=check, passed=False, error=f"read failed: {exc}")
        _scan(fp, text)
    for src in brain_sources:
        _scan(
            src.get("locus") or "brain://?",
            src.get("text") or "",
            subtask_id=src.get("subtask_id"),
        )
    if hits:
        findings.append({
            **hits[0],                      # file/line/subtask_id of the first hit
            "term": term,
            "replacement": replacement,
            "occurrences": hits,            # every locus, for autofix + display
            "occurrence_count": len(hits),
            "suggested_fix": _term_fix_text(term, replacement, check.origin, len(hits)),
        })
    return CheckResult(check=check, passed=not findings, findings=findings)


def autofix_term_consistency(
    det_results: list[CheckResult],
    *,
    created_by: str = "reviewer-autofix",
) -> int:
    """Pillar 1 — auto-apply ADR/decision terminology substitutions to brain
    Stream-B artifacts instead of FAILing → cold subagent re-dispatch.

    ``term_consistency`` findings carry the EXACT ``(term → replacement)`` plus a
    word-boundary regex, so the fix is fully deterministic — round-tripping a
    whole subagent just to perform a string replacement is pure waste (it was
    ~43% of all load-bearing FAILs and a dominant re-review loop driver). For
    each FAILED term_consistency check we group findings by their owning brain
    artifact (locus ``brain://<id>``), apply the replacement to the body
    (skipping rejection-cue lines EXACTLY as :func:`_run_term_consistency` does,
    so a line that DESCRIBES the rejection is never rewritten), and persist a
    corrected, superseding artifact. The caller re-runs the deterministic stage
    against the corrected bodies.

    Only brain artifacts are auto-fixed; filesystem-locus findings are left to
    the author (never silently rewrite a user's repo file). Best-effort: a
    read/write failure on one artifact is logged and skipped, never raised — the
    pipeline degrades to the legacy FAIL→re-dispatch path. Returns the number of
    artifacts rewritten.
    """
    by_aid: dict[str, list[tuple[str, str]]] = {}
    for r in det_results:
        if r.passed or r.check.kind != "term_consistency":
            continue
        for f in r.findings or []:
            locus = str((f or {}).get("file") or "")
            if not locus.startswith("brain://"):
                continue
            aid = locus[len("brain://"):].split(" ")[0].strip()
            term = (f or {}).get("term")
            repl = (f or {}).get("replacement")
            if not aid or not term or repl is None:
                continue
            # P1 corruption guard — ONLY auto-rewrite short canonical tokens.
            # A verbose/numeric/value replacement (e.g. a whole decision
            # sentence "B_10Y=10,823 (within seq-75 …)") must never be
            # substituted for a token — that doubled/garbled the deliverable
            # (task-20260623-213417 2.3/2.5). Such findings still FAIL and fall
            # back to the legacy author re-dispatch path; they are just never
            # auto-applied.
            if not (_is_safe_autofix_token(term) and _is_safe_autofix_token(repl)):
                continue
            pairs = by_aid.setdefault(aid, [])
            if (term, repl) not in pairs:
                pairs.append((term, repl))
    if not by_aid:
        return 0

    try:
        from okuro.sense.artifacts import artifact_get, artifact_write
    except Exception as exc:  # pragma: no cover - import guard
        log.warning("autofix_term_consistency: artifacts API import failed: %s", exc)
        return 0

    fixed = 0
    for aid, pairs in by_aid.items():
        try:
            art = artifact_get(aid, include_body=True) or {}
        except Exception as exc:
            log.warning("autofix: artifact_get(%s) failed: %s", aid, exc)
            continue
        body = art.get("body") or ""
        if not body:
            continue
        out_lines: list[str] = []
        changed = False
        for line in body.splitlines(keepends=True):
            # Same false-positive guards as the check, in the same order:
            # never rewrite a line that is talking ABOUT the change, and never
            # rewrite inside a non-prose span (a URL slug or a code span is the
            # one place a rename must NOT be applied — it breaks the citation).
            subs = [
                (_term_regex(term), repl)
                for term, repl in pairs
                if not _line_documents_change(line, (_term_regex(repl),))
            ]
            new_line = _sub_in_prose(line, subs) if subs else line
            changed = changed or (new_line != line)
            out_lines.append(new_line)
        if not changed:
            continue
        try:
            new_id = artifact_write(
                kind=art.get("kind") or "report",
                title=art.get("title") or aid,
                summary=art.get("summary"),
                body="".join(out_lines),
                supersedes=aid,
                task_id=art.get("task_id"),
                subtask_id=art.get("subtask_id"),
                created_by=created_by,
                confidence=0.85,
            )
        except Exception as exc:
            log.warning("autofix: artifact_write(supersedes=%s) failed: %s", aid, exc)
            continue
        if isinstance(new_id, str) and new_id.startswith("REJECTED"):
            log.warning("autofix: artifact_write rejected for %s: %s", aid, new_id)
            continue
        fixed += 1
        log.info(
            "autofix: applied %d terminology fix(es) to artifact %s -> %s",
            len(pairs), aid, new_id,
        )
    return fixed


def _file_exists_candidates(
    *,
    path: str,
    task_id: str,
    subtask_id: str,
    project_path: str,
    tasks_dir: str,
    artifact_name: str = "",
) -> list[Path]:
    """Build the ordered list of plausible filesystem locations for an
    `outputs[]` / `target_paths[]` declaration. First hit ends the check.

    Strategy (DP10 — every legitimate write path has a candidate):
      1. As declared (literal). Catches absolute paths.
      2. Project-path relative. Catches code targets.
      3. Task artifacts dir, bare name. Catches artifact_write with the
         declared artifact_name.
      4. Task artifacts dir, subtask-id-prefixed. Catches the dispatcher's
         legacy save_artifact path `{subtask_id}-{name}.md`.
      5. Task artifacts dir, subtask-id-underscore-prefixed (`{id}_name`).
         Catches another observed subagent variant.
    """
    cands: list[Path] = []
    p = Path(path)
    cands.append(p)
    if project_path and not p.is_absolute():
        cands.append(Path(project_path) / p)
    if task_id and tasks_dir:
        task_artifacts = Path(tasks_dir) / task_id / "artifacts"
        bare = p.name
        cands.append(task_artifacts / p)
        cands.append(task_artifacts / bare)
        if subtask_id and subtask_id != "?":
            cands.append(task_artifacts / f"{subtask_id}-{bare}")
            cands.append(task_artifacts / f"{subtask_id}_{bare}")
        # artifact_name-derived candidates. The planner's outputs[0] can
        # disagree with artifact_name (e.g. "foo-comparison.md" declared
        # but "foo-survey" written) — accept either spelling.
        if artifact_name:
            stem = artifact_name[:-3] if artifact_name.endswith(".md") else artifact_name
            for ext in (".md", ".html", ""):
                aname = f"{stem}{ext}" if ext else stem
                cands.append(task_artifacts / aname)
                if subtask_id and subtask_id != "?":
                    cands.append(task_artifacts / f"{subtask_id}-{aname}")
                    cands.append(task_artifacts / f"{subtask_id}_{aname}")
    return cands


def _file_exists_in_brain(
    target: str | None = None,
    artifact_rows: list[dict] | None = None,
    *,
    path: str | None = None,
    task_id: str = "",
    subtask_id: str = "",
) -> bool:
    """Match against the brain artifact registry. Subagents that called
    `artifact_write(...)` land here even when no file was written under
    `outputs[]` literally — the artifact name's stem is the load-bearing
    identity, not the path.

    Two call shapes (Gate 2 §C11 / G4):

    1. ``_file_exists_in_brain(target, artifact_rows)`` — pure helper.
       Matches ``target`` against rows already loaded from
       :func:`artifact_list`. No DB call. This is the form the C11
       correctness tests use, and the form callers that already hold
       the row list prefer.

    2. ``_file_exists_in_brain(path=..., task_id=..., subtask_id=...)``
       — legacy DB-fetching form. Loads rows via :func:`artifact_list`
       then delegates to form #1.

    Normalization (G4) — both sides collapse to
    ``re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')``. This kills the
    "RT Baseline" vs "rt-baseline" hyphen-vs-space mismatch that defeated
    the rescue path on the audio-stack subtask (audit-03 Note A) and
    every similar title-casing drift.
    """
    if target is None:
        target = path or ""
    if not target:
        return False

    if artifact_rows is None:
        if not task_id:
            return False
        try:
            from okuro.sense.artifacts import artifact_list
        except Exception:
            return False
        try:
            artifact_rows = artifact_list(
                task_id=task_id, subtask_id=subtask_id, limit=50,
            )
        except Exception:
            return False

    import re as _re

    def _norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")

    stem_n = _norm(Path(target).stem)
    bare_n = _norm(Path(target).name)
    for r in artifact_rows or []:
        title_n = _norm(r.get("title") or "")
        name_n = _norm(r.get("name") or "")
        # Symmetric containment — either side may carry the other's stem.
        # "RT Baseline" → title_n="rt-baseline"; target "rt-baseline.md" →
        # stem_n="rt-baseline" → stem_n in title_n is True.
        if stem_n and (stem_n in title_n or title_n in stem_n
                       or stem_n in name_n or name_n in stem_n):
            return True
        if bare_n and (bare_n == name_n or bare_n in title_n):
            return True
    return False


def _run_file_exists(check: Check) -> CheckResult:
    path = check.params["path"]
    task_id = check.params.get("task_id") or ""
    subtask_id = check.params.get("subtask_id") or ""
    project_path = check.params.get("project_path") or ""
    tasks_dir = check.params.get("tasks_dir") or str(_resolve_tasks_dir())
    artifact_name = check.params.get("artifact_name") or ""
    for c in _file_exists_candidates(
        path=path, task_id=task_id, subtask_id=subtask_id,
        project_path=project_path, tasks_dir=tasks_dir,
        artifact_name=artifact_name,
    ):
        if c.exists() and c.is_file():
            return CheckResult(check=check, passed=True)
    if _file_exists_in_brain(path=path, task_id=task_id, subtask_id=subtask_id):
        return CheckResult(check=check, passed=True)
    return CheckResult(check=check, passed=False, findings=[{
        "file": path,
        "subtask_id": subtask_id or check.params.get("subtask_id"),
        "evidence": "declared output not found on disk or in brain registry",
        "suggested_fix": (
            f"Create {path} under task artifacts dir or via artifact_write, "
            f"or remove the entry from subtask outputs."
        ),
    }])


def _run_link_integrity(check: Check) -> CheckResult:
    fp = Path(check.params["file"])
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return CheckResult(check=check, passed=False, error=f"read failed: {exc}")
    findings: list[dict] = []
    base = fp.parent
    suffix = fp.suffix.lower()
    pat = _MD_LINK_RE if suffix == ".md" else _HTML_HREF_RE
    for i, line in enumerate(text.splitlines(), 1):
        for m in pat.finditer(line):
            target = m.group(1).split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:", "javascript:", "tel:", "data:", "#")):
                continue
            if target.startswith("/"):
                continue  # absolute web paths — out of scope for static check
            resolved = (base / target).resolve()
            if not resolved.exists():
                findings.append({
                    "file": str(fp),
                    "line": i,
                    "evidence": line.strip()[:200],
                    "broken_link": target,
                    "suggested_fix": f"Fix or remove broken reference to {target!r}",
                })
    return CheckResult(check=check, passed=not findings, findings=findings)


def _run_schema_assert(check: Check) -> CheckResult:
    p = Path(check.params["path"])
    expected = check.params["expected_suffix"]
    if not p.exists():
        # Handled by file_exists; not our concern.
        return CheckResult(check=check, passed=True)
    actual = p.suffix.lower()
    if actual != expected:
        return CheckResult(check=check, passed=False, findings=[{
            "file": str(p),
            "evidence": f"on-disk suffix {actual!r} != declared {expected!r}",
            "suggested_fix": f"Rename to match declared extension or update subtask.outputs",
        }])
    return CheckResult(check=check, passed=True)


def _run_secret_name(check: Check) -> CheckResult:
    known = set(check.params["known_names"])
    brain_sources: list[dict] = check.params.get("brain_sources") or []
    findings: list[dict] = []

    def _scan(locus: str, text: str, subtask_id: Optional[str] = None) -> None:
        for i, line in enumerate(text.splitlines(), 1):
            for m in _KEYRING_RE.finditer(line):
                name = m.group(1)
                if name in known:
                    continue
                f = {
                    "file": locus,
                    "line": i,
                    "evidence": line.strip()[:200],
                    "secret_name": name,
                    "known_names": sorted(known),
                    "suggested_fix": (
                        f"Secret {name!r} not in active set "
                        f"{sorted(known)} — typo, alias, or new secret missing an ADR?"
                    ),
                }
                if subtask_id:
                    f["subtask_id"] = subtask_id
                findings.append(f)

    fp_param = check.params.get("file")
    if fp_param:
        fp = Path(fp_param)
        try:
            _scan(str(fp), fp.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            return CheckResult(check=check, passed=False, error=f"read failed: {exc}")
    for src in brain_sources:
        _scan(
            src.get("locus") or "brain://?",
            src.get("text") or "",
            subtask_id=src.get("subtask_id"),
        )
    return CheckResult(check=check, passed=not findings, findings=findings)


def _run_ac_evidence_shape(check: Check) -> CheckResult:
    """Fetch the subtask's artifact body, scan for §C11-shaped evidence."""
    task_id = check.params.get("task_id") or ""
    subtask_id = check.params.get("subtask_id") or ""
    acs: list[str] = list(check.params.get("acceptance_criteria") or [])
    if not task_id or not subtask_id or not acs:
        return CheckResult(check=check, passed=True)

    try:
        from okuro.sense.artifacts import artifact_list, artifact_get
    except Exception as exc:
        # Brain unavailable — treat as pass (no evidence to judge against).
        return CheckResult(check=check, passed=True, error=f"brain import failed: {exc}")

    try:
        rows = artifact_list(
            task_id=task_id, subtask_id=subtask_id, limit=20,
            include_superseded=False,
        )
    except Exception as exc:
        return CheckResult(check=check, passed=True, error=f"artifact_list failed: {exc}")

    if not rows:
        # No artifact yet → file_exists handles the missing-deliverable case
        # at load-bearing severity. We pass to avoid double-flagging.
        return CheckResult(check=check, passed=True)

    # Concatenate all (current) artifact bodies for this subtask. Most
    # subtasks produce one artifact; concatenating handles the edge case
    # where multiple parts of the deliverable live in sibling artifacts.
    body_parts: list[str] = []
    for r in rows:
        aid = r.get("id") or ""
        if not aid:
            continue
        try:
            full = artifact_get(aid, include_body=True) or {}
        except Exception:
            continue
        body = full.get("body") or ""
        if body:
            body_parts.append(body)
    if not body_parts:
        return CheckResult(check=check, passed=True)

    artifact_text = "\n\n".join(body_parts)
    findings = _check_ac_evidence_shape(artifact_text, acs)
    if not findings:
        return CheckResult(check=check, passed=True)
    # Stamp each finding with the subtask id so the engine's retry logic
    # can implicate the right subtask. ``file`` is left empty — this is a
    # body-shape defect, not a file defect.
    for f in findings:
        f.setdefault("subtask_id", subtask_id)
    return CheckResult(check=check, passed=False, findings=findings)


def _run_numeric_consistency(check: Check) -> CheckResult:
    """Recompute every ```numbers ledger in the deliverable text. Pure +
    deterministic — no LLM, no DB. Findings carry the exact discrepancy so the
    blocked_review worklist tells the author the precise number to correct."""
    from okuro.orchestrator.reviewer.numeric_ledger import reconcile_text

    files: list[str] = check.params.get("files") or []
    brain_sources: list[dict] = check.params.get("brain_sources") or []
    findings: list[dict] = []

    def _collect(locus: str, text: str, subtask_id: Optional[str] = None) -> None:
        for f in reconcile_text(text):
            f = dict(f)
            f["file"] = locus
            if subtask_id:
                f.setdefault("subtask_id", subtask_id)
            f.setdefault("suggested_fix", "Correct the ledger so its totals reconcile.")
            findings.append(f)

    for fp in files:
        try:
            text = Path(fp).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return CheckResult(check=check, passed=False, error=f"read failed: {exc}")
        _collect(fp, text)
    for src in brain_sources:
        _collect(
            src.get("locus") or "brain://?",
            src.get("text") or "",
            subtask_id=src.get("subtask_id"),
        )
    return CheckResult(check=check, passed=not findings, findings=findings)


def _run_brain_disk_divergence(check: Check) -> CheckResult:
    """Compare the on-disk file's mtime against its brain artifact's write time.

    Passes when: the file is absent (``file_exists`` owns that finding — two
    checks reporting one missing file is noise), no brain artifact matches, or
    the artifact is at least as new as the file.
    """
    import datetime as _dt

    path = check.params["path"]
    task_id = check.params.get("task_id") or ""
    subtask_id = check.params.get("subtask_id") or ""

    disk: Optional[Path] = None
    for c in _file_exists_candidates(
        path=path, task_id=task_id, subtask_id=subtask_id,
        project_path=check.params.get("project_path") or "",
        tasks_dir=check.params.get("tasks_dir") or str(_resolve_tasks_dir()),
        artifact_name=check.params.get("artifact_name") or "",
    ):
        if c.exists() and c.is_file():
            disk = c
            break
    if disk is None:
        return CheckResult(check=check, passed=True)

    try:
        from okuro.sense.artifacts import artifact_list

        rows = artifact_list(task_id=task_id, subtask_id=subtask_id, limit=25)
    except Exception:
        return CheckResult(check=check, passed=True)

    stem = Path(path).name.lower()
    written: Optional[str] = None
    for row in rows or []:
        title = str((row or {}).get("title") or "").lower()
        if stem and (stem in title or Path(stem).stem in title):
            written = str((row or {}).get("updated_at") or (row or {}).get("created_at") or "")
            break
    if not written:
        return CheckResult(check=check, passed=True)

    try:
        # Artifact timestamps are naive UTC, like every okuro timestamp — so
        # the disk mtime is converted to UTC rather than local. Comparing a
        # naive-UTC string against a local-time reading is the class that has
        # bitten this plan three times.
        disk_utc = _dt.datetime.utcfromtimestamp(disk.stat().st_mtime)
        artifact_ts = _dt.datetime.fromisoformat(written.replace("Z", ""))
    except (OSError, ValueError):
        return CheckResult(check=check, passed=True)

    # A small grace: the artifact write and the file write are seconds apart
    # in the normal case, in either order.
    if (disk_utc - artifact_ts).total_seconds() <= 120:
        return CheckResult(check=check, passed=True)

    return CheckResult(check=check, passed=False, findings=[{
        "file": path,
        "subtask_id": subtask_id,
        "evidence": (
            f"disk file is {int((disk_utc - artifact_ts).total_seconds())}s newer "
            f"than its brain artifact ({artifact_ts.isoformat()}) — the review "
            "reads the artifact body, so it is grading a stale copy"
        ),
        "suggested_fix": (
            "re-run artifact_write for this deliverable so the brain body "
            "matches what is on disk, or drop the disk copy if the artifact "
            "is the deliverable"
        ),
    }])


_RUNNERS = {
    "term_consistency": _run_term_consistency,
    "numeric_consistency": _run_numeric_consistency,
    "file_exists": _run_file_exists,
    "link_integrity": _run_link_integrity,
    "schema_assert": _run_schema_assert,
    "secret_name": _run_secret_name,
    "ac_evidence_shape": _run_ac_evidence_shape,
    "brain_disk_divergence": _run_brain_disk_divergence,
}


def run_checks(checks: Iterable[Check]) -> list[CheckResult]:
    """Run each check via its registered runner. Pure — no LLM, no DB."""
    results: list[CheckResult] = []
    for c in checks:
        runner = _RUNNERS.get(c.kind)
        if runner is None:
            results.append(CheckResult(
                check=c,
                passed=False,
                error=f"no runner registered for kind={c.kind!r}",
            ))
            continue
        try:
            results.append(runner(c))
        except Exception as exc:  # runner crash must not abort the chain
            log.exception("runner crashed for check kind=%s", c.kind)
            results.append(CheckResult(check=c, passed=False, error=f"runner crashed: {exc}"))
    return results


def summarize(results: list[CheckResult]) -> dict:
    """Counts + load-bearing-failure flag — used by pipeline.run_review."""
    total = len(results)
    failed = [r for r in results if not r.passed]
    load_bearing_fails = [r for r in failed if r.check.severity == "load_bearing"]
    finding_count = sum(len(r.findings) for r in results)
    return {
        "total_checks": total,
        "failed_checks": len(failed),
        "load_bearing_failures": len(load_bearing_fails),
        "total_findings": finding_count,
        "short_circuit": bool(load_bearing_fails),
    }
