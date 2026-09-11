# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Unified budget-aware brain retrieval shared by every artifact generator.
# index: def _fit | def gather_context
# AGENT_HEADER_END -->
"""Unified budget-aware retrieval over okuro's brain.

ONE retriever for every artifact generator — podcast, slides, video, flow,
brain-advise — so grounding is consistent everywhere instead of each feature
carrying its own divergent ``_gather_context`` copy.

Two defects this replaces:
  1. Flat per-artifact head-truncation (``body[:1800]``). Long reports put the
     recommendation/conclusion LAST, so head-truncation silently dropped the
     most important part. ``_fit`` keeps the head AND the tail.
  2. No shared budget. The char ``budget`` is split across the selected
     sources by weight; a single dominant report gets most of it instead of a
     fixed tiny slice.

Pure + best-effort: each source is independent and never raises out — a
missing module or empty result just contributes nothing.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Iterable, Optional

log = logging.getLogger("okuro.sense.grounding")

# Source identifiers — callers pick the subset they want.
SOURCE_ARTIFACTS = "artifacts"   # canonical reports / plans / evidence (full bodies)
SOURCE_MEMORY = "memory"         # agent memory digest
SOURCE_PROGRESS = "progress"     # recent progress log for a project
SOURCE_THOUGHTS = "thoughts"     # surfaced user thoughts
SOURCE_PROFILE = "profile"       # user profile (yaml)
SOURCE_PROJECT = "project"       # project record

# What podcast/slides/video want by default: the brain's durable knowledge.
DEFAULT_SOURCES = (SOURCE_ARTIFACTS, SOURCE_MEMORY, SOURCE_PROGRESS)

# Relative share of the char budget per source (normalised over the selected
# subset). Artifacts dominate — they're the canonical synthesized knowledge.
_WEIGHTS = {
    SOURCE_ARTIFACTS: 0.55,
    SOURCE_MEMORY: 0.20,
    SOURCE_PROGRESS: 0.10,
    SOURCE_THOUGHTS: 0.05,
    SOURCE_PROFILE: 0.05,
    SOURCE_PROJECT: 0.05,
}

# Never starve an artifact below this — a 300-char slice (the old 1800 was
# already too small for a real report) is useless; better to slightly exceed
# the nominal budget than to gut the one report that matters.
_MIN_ARTIFACT_CHARS = 1500


def _fit(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars keeping BOTH ends.

    Reports lead with context and close with the recommendation; a plain
    head-cut drops the conclusion. Keep ~70% head + ~30% tail with a marker so
    the model sees where the body opens and how it lands."""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    if limit < 400:  # too small to split meaningfully
        return text[:limit].rstrip()
    head = int(limit * 0.7)
    tail = limit - head - 16
    return f"{text[:head].rstrip()}\n\n…[trimmed]…\n\n{text[-tail:].lstrip()}"


def gather_context(
    query: str,
    *,
    budget: int = 9000,
    project: Optional[str] = None,
    sources: Iterable[str] = DEFAULT_SOURCES,
    exclude_titles: Iterable[str] = (),
    max_artifacts: int = 3,
    web: bool = False,
    web_timeout: int = 240,
    progress_cb: Optional[Callable[[str, float], None]] = None,
) -> tuple[str, list[str]]:
    """Retrieve a budget-bounded markdown context for ``query`` + its sources.

    Args:
        query: the topic / task to ground.
        budget: total char budget, split across ``sources`` by weight.
        project: scope memory/progress to this project (cuts cross-project noise).
        sources: which brain sources to include (see SOURCE_* constants).
        exclude_titles: artifact titles to skip (e.g. a generator skipping the
            artifact it is about to write, so it never feeds on itself).
        max_artifacts: cap on retrieved artifacts; the artifact budget is split
            across however many come back (min ``_MIN_ARTIFACT_CHARS`` each).
        web: opt-in short web search (slow, multi-minute deep-research provider).
        progress_cb: optional (label, fraction) sink for UI progress.

    Returns (context_markdown, source_labels). Empty context => caller should
    fall back to flagged general knowledge.
    """
    sset = {s for s in sources}
    weights = {k: v for k, v in _WEIGHTS.items() if k in sset}
    total_w = sum(weights.values()) or 1.0

    def cap(source: str) -> int:
        return int(budget * _WEIGHTS.get(source, 0.1) / total_w)

    def report(label: str, frac: float) -> None:
        if progress_cb:
            try:
                progress_cb(label, frac)
            except Exception:
                pass

    parts: list[str] = []
    sources_used: list[str] = []
    excl = {t.strip().lower() for t in exclude_titles if t and t.strip()}

    # 1 — artifacts (canonical reports/plans/evidence), budget-split full bodies.
    if SOURCE_ARTIFACTS in sset:
        report("researching okuro brain", 0.05)
        try:
            from okuro.sense.artifacts import artifact_get, artifact_search

            rows = artifact_search(query, limit=max(5, max_artifacts)) or []
            picked = [r for r in rows if (r.get("title") or "").strip().lower() not in excl][:max_artifacts]
            if picked:
                per = max(cap(SOURCE_ARTIFACTS) // len(picked), _MIN_ARTIFACT_CHARS)
                for a in picked:
                    body = ""
                    try:
                        full = artifact_get(a["id"])
                        if isinstance(full, dict):
                            body = full.get("body") or ""
                    except Exception:
                        pass
                    snippet = _fit(body or a.get("summary") or "", per)
                    if snippet:
                        parts.append(f"## {a.get('kind', 'artifact')}: {a.get('title', '')}\n{snippet}")
                        sources_used.append(f"artifact:{(a.get('title') or '')[:48]}")
        except Exception as exc:
            log.info("grounding: artifacts failed: %s", exc)

    # 2 — agent memory digest.
    if SOURCE_MEMORY in sset:
        try:
            from okuro.sense.memory import read_memory

            mem = read_memory(query=query, project=project, limit=10)
            if isinstance(mem, str) and mem.strip() and "no relevant" not in mem.lower()[:80] and "no memories" not in mem.lower()[:80]:
                parts.append(f"## Memory\n{_fit(mem, cap(SOURCE_MEMORY))}")
                sources_used.append("memory")
        except Exception as exc:
            log.info("grounding: memory failed: %s", exc)

    # 3 — recent progress (richest for self/"developments" topics).
    if SOURCE_PROGRESS in sset and project:
        try:
            from okuro.sense.progress import get_progress

            prog = get_progress(project, include_history=True)
            if prog:
                parts.append(f"## Recent progress — {project}\n{_fit(str(prog), cap(SOURCE_PROGRESS))}")
                sources_used.append(f"progress:{project}")
        except Exception as exc:
            log.info("grounding: progress failed: %s", exc)

    # 4 — user profile.
    if SOURCE_PROFILE in sset:
        try:
            from okuro.yu.profile import get_profile

            prof = get_profile(format="yaml")
            if prof and prof.strip():
                parts.append(f"## Profile\n{_fit(prof, cap(SOURCE_PROFILE))}")
                sources_used.append("profile")
        except Exception as exc:
            log.info("grounding: profile failed: %s", exc)

    # 5 — project record (slug derived from the query words when not given).
    if SOURCE_PROJECT in sset:
        try:
            from okuro.sense.projects import get_project

            slug = project
            rec = get_project(slug) if slug else None
            if not rec:
                for word in query.lower().split():
                    rec = get_project(word)
                    if rec:
                        break
            if rec:
                parts.append(f"## Project\n{_fit(str(rec), cap(SOURCE_PROJECT))}")
                sources_used.append("project")
        except Exception as exc:
            log.info("grounding: project failed: %s", exc)

    # 6 — surfaced thoughts.
    if SOURCE_THOUGHTS in sset:
        try:
            from okuro.sense.thoughts import surface_relevant

            thoughts = surface_relevant(query, limit=3)
            if thoughts and str(thoughts).strip():
                parts.append(f"## Thoughts\n{_fit(str(thoughts), cap(SOURCE_THOUGHTS))}")
                sources_used.append("thoughts")
        except Exception as exc:
            log.info("grounding: thoughts failed: %s", exc)

    # 7 — OPTIONAL short web search for current external facts (opt-in, slow).
    if web:
        try:
            report("researching web", 0.15)
            from okuro.bridge.invoke import invoke

            r = invoke(
                "Do a SHORT web search and return 4-6 bullet points of current, "
                "verifiable, cited facts about this topic. Facts only, no preamble:\n"
                f"{query}",
                capability="deep-research",
                timeout=web_timeout,
            )
            if isinstance(r, dict) and r.get("success") and (r.get("output") or "").strip():
                parts.append(f"## Web findings (current)\n{_fit(r['output'], 2000)}")
                sources_used.append("web")
        except Exception as exc:
            log.info("grounding: web failed: %s", exc)

    return "\n\n".join(parts), sources_used
