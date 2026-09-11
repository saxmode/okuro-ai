# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Proactive model suggestions — shortlist fit-gated candidates for this
#          box, research the top few against the user's goals (LLM), and publish
#          rationale'd suggestions into okuro's signals surface (Now page / inbox
#          / daily digest). The "investigated, researched and suggested" layer
#          on top of on-demand discovery.
# index:
#   class ModelSuggestion
#   def shortlist            (discover + rank → top-k, injectable)
#   def research             (LLM rationale, degrades to deterministic)
#   def build_suggestion
#   def suggest_models       (orchestration)
#   def publish              (dedup + signal_add into the signals queue)
# AGENT_HEADER_END -->
"""Hybrid model recommender: cheap rank() shortlists what the box can run, then
an agent researches only the top few against the user's goals — why it's worth
running, the use-case, the caveat — and files each as a ``proactive`` signal.

Injectable seams (``discover_fn`` / ``invoke_fn``) keep the orchestration pure
and unit-testable without network or a live provider. The research step degrades
gracefully to a deterministic rationale when no provider is configured, so a
suggestion is always producible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import catalog


@dataclass
class ModelSuggestion:
    """One researched, box-fit model recommendation."""

    catalog_id: str
    display_name: str
    modality: str
    min_vram_gb: float
    fit_gpu: str            # named GPU it fits on (e.g. "GPU1 24GB")
    score: float
    rationale: str          # why it's worth running FOR THIS user
    use_case: str
    caveats: str
    source_url: str = ""
    researched: bool = False  # True = LLM rationale, False = deterministic
    commercial_status: str = "unknown"   # commercial|conditional|non_commercial|unknown
    commercial_allowed: bool = False     # conservative gate decision
    license_id: Optional[str] = None
    evidence: dict = field(default_factory=dict)

    def to_evidence(self) -> dict:
        return {
            # Names this producer for the signal closure registries
            # (sense.signals.producer_id). Rows written before this tag
            # existed are resolved from the source_ref prefix instead.
            "scanner": "model_suggest",
            "catalog_id": self.catalog_id,
            "modality": self.modality,
            "min_vram_gb": self.min_vram_gb,
            "fit_gpu": self.fit_gpu,
            "score": self.score,
            "rationale": self.rationale,
            "use_case": self.use_case,
            "caveats": self.caveats,
            "url": self.source_url,
            "researched": self.researched,
            "commercial_status": self.commercial_status,
            "commercial_allowed": self.commercial_allowed,
            "license_id": self.license_id,
        }


def _fit_gpu(min_vram_gb: float, detection: dict) -> str:
    """Smallest non-display GPU that fits, as 'NAME NNgb' (or 'none')."""
    gpus = sorted(
        (g for g in detection.get("gpus", []) if not g.get("is_display")),
        key=lambda g: float(g.get("vram_gb", 0) or 0),
    )
    for g in gpus:
        if float(g.get("vram_gb", 0) or 0) >= min_vram_gb:
            name = g.get("name", "GPU")
            return f"{name} {int(float(g['vram_gb']))}GB"
    return "none"


def shortlist(
    detection: dict,
    *,
    query: Optional[str] = None,
    modality: str = "text",
    k: int = 5,
    sort: str = "popular",
    discover_fn: Optional[Callable] = None,
):
    """Discover candidates and rank them for this box → top-k CatalogEntry.

    ``sort='new'`` gathers recently-published models (the 'what's new' feed);
    rank() still fit-gates + orders, so only new AND runnable AND decent-quality
    models survive.
    """
    if discover_fn is None:
        from .discovery import discover as discover_fn  # lazy: network client
    ents = discover_fn(query=query, modality=modality, limit=max(k * 4, 20), sort=sort)
    return catalog.rank(ents, detection, modality=modality)[:k]


_RESEARCH_SYS = (
    "You advise the operator of an agent-native OS who runs open models on "
    "their own local GPU hardware. Judge whether a specific open model is "
    "worth running LOCALLY for that mission. Be concrete and "
    "skeptical — no hype. Reply as strict JSON with keys rationale, use_case, "
    "caveats (each one short sentence)."
)


def _research_prompt(entry, goals: str) -> str:
    best = entry.best_runnable_format
    return (
        f"Model: {entry.display_name}\n"
        f"Task: {entry.task} · modality: {entry.modality} · family: {entry.family or '?'}\n"
        f"~VRAM: {entry.min_vram_gb:.1f}GB · downloads: {entry.quality_signals.get('downloads', 0)}\n"
        f"Base: {entry.base_model or '?'} · params: {getattr(best, 'param_b', None)}\n"
        f"User goals: {goals}\n\n"
        f"Is this worth running locally, and for what? JSON only."
    )


def research(entry, goals: str, *, invoke_fn: Optional[Callable] = None) -> dict:
    """Research one candidate → {rationale, use_case, caveats, researched}.

    Uses the bridge to ask a model; degrades to a deterministic rationale
    (purpose summary) when no provider answers, so this never blocks.
    """
    if invoke_fn is None:
        from okuro.bridge import invoke as invoke_fn  # lazy: provider layer
    try:
        res = invoke_fn(
            _research_prompt(entry, goals),
            capability="standard",
            system_prompt=_RESEARCH_SYS,
            timeout=60,
        )
        if res.get("success") and res.get("output"):
            parsed = _parse_research(res["output"])
            if parsed:
                parsed["researched"] = True
                return parsed
    except Exception:
        pass
    # deterministic fallback — always available
    return {
        "rationale": catalog.derive_purpose_summary(entry),
        "use_case": entry.task.replace("-", " "),
        "caveats": "auto-summary (no live research)",
        "researched": False,
    }


def _parse_research(text: str) -> Optional[dict]:
    """Extract {rationale, use_case, caveats} from a model's JSON-ish reply."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    out = {k: _flatten(data.get(k, "")) for k in ("rationale", "use_case", "caveats")}
    return out if out.get("rationale") else None


def _flatten(value) -> str:
    """Coerce a JSON value to one clean sentence — a model may return a caveat
    as a string OR a list; a bare list-repr in the UI reads as a bug."""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def build_suggestion(entry, detection: dict, researched: dict) -> ModelSuggestion:
    from . import licensing

    # Commercial-use verdict at qualification — recorded so the UI can flag a
    # model before it's pulled/run. org_revenue unknown → conditional/none gate
    # to not-allowed (conservative); the status still shows the real tier.
    verdict = licensing.commercial_status(
        entry.license, family=entry.family, base_model=entry.base_model,
    )
    return ModelSuggestion(
        catalog_id=entry.catalog_id,
        display_name=entry.display_name,
        modality=entry.modality,
        min_vram_gb=round(entry.min_vram_gb, 1),
        fit_gpu=_fit_gpu(entry.min_vram_gb, detection),
        score=catalog.score_quality(entry),
        rationale=researched.get("rationale", ""),
        use_case=researched.get("use_case", ""),
        caveats=researched.get("caveats", ""),
        source_url=(entry.provenance or {}).get("url", ""),
        researched=bool(researched.get("researched")),
        commercial_status=verdict["status"],
        commercial_allowed=verdict["allowed"],
        license_id=verdict["license_id"],
    )


def suggest_models(
    detection: dict,
    goals: str,
    *,
    query: Optional[str] = None,
    modality: str = "text",
    k: int = 5,
    sort: str = "popular",
    discover_fn: Optional[Callable] = None,
    invoke_fn: Optional[Callable] = None,
) -> list[ModelSuggestion]:
    """Shortlist → research each → ModelSuggestion list (unpublished)."""
    picks = shortlist(detection, query=query, modality=modality, k=k, sort=sort, discover_fn=discover_fn)
    return [
        build_suggestion(e, detection, research(e, goals, invoke_fn=invoke_fn))
        for e in picks
    ]


def publish(
    suggestions: list[ModelSuggestion],
    *,
    existing_refs: Optional[set] = None,
    signal_add_fn: Optional[Callable] = None,
    upsert_fn: Optional[Callable] = None,
) -> list[dict]:
    """Persist each suggestion to the durable discoveries list AND file a
    proactive signal for genuinely-new ones.

    Two surfaces, two lifetimes: the ``model_discoveries`` row is the durable
    LIST the Discover tab reads (upserted every scan, survives dismissal); the
    signal is the one-shot NOTIFICATION (filed only when the model is new, i.e.
    not already an open signal / acquired bundle).

    ``existing_refs`` = catalog_ids already suggested/acquired (caller supplies,
    or we derive from open signals + acquired bundles). Returns the created
    signal rows.
    """
    if signal_add_fn is None:
        from okuro.sense.signals import signal_add as signal_add_fn
    if upsert_fn is None:
        from .discoveries import upsert_discovery as upsert_fn
    if existing_refs is None:
        existing_refs = _known_refs()

    created: list[dict] = []
    for s in suggestions:
        # Durable list row first — always upsert so the tab reflects the
        # latest scan even for already-notified/acknowledged models. A
        # persistence hiccup must never block the notification path.
        try:
            upsert_fn(s)
        except Exception:
            pass
        if s.catalog_id in existing_refs:
            continue
        row = signal_add_fn(
            source="proactive",
            severity="info",
            summary=f"Run locally? {s.display_name} — {s.rationale}"[:280],
            source_ref=s.catalog_id,
            evidence=s.to_evidence(),
            suggested_action=f"okuro models pull {s.catalog_id}",
        )
        created.append(row)
        existing_refs.add(s.catalog_id)
    return created


_DEFAULT_GOALS = (
    "Build an agent-native OS; local LLM inference, agent orchestration, "
    "tool-use and coding assistants; run everything locally on the box."
)


def _profile_goals() -> str:
    """The user's goal string for research context — profile main_goal +
    working areas, falling back to a sensible default."""
    try:
        from okuro.yu.profile import get_profile_raw

        p = get_profile_raw() or {}
        goal = (p.get("identity") or {}).get("main_goal")
        strong = ", ".join((p.get("expertise") or {}).get("strong", [])[:4])
        if goal:
            return f"{goal}." + (f" Strengths: {strong}." if strong else "")
    except Exception:
        pass
    return _DEFAULT_GOALS


def run_suggestions(*, modality: str = "text", k: int = 5, sort: str = "trending") -> str:
    """Daemon entrypoint (okuro.ai_models.suggest:run_suggestions).

    Detect the box, discover fresh-and-reviewed fit models (``sort='trending'``
    — models gaining traction now, the "new AND greatly reviewed" feed; raw
    'new' is a firehose of test uploads that the quality gate rightly drops to
    zero), research the top-k against the user's goals, and publish new ones as
    proactive signals + durable discoveries. Returns a one-line summary for the
    scheduler log. Never raises on a research/publish hiccup.
    """
    from .edition import detect_edition, effective_detection, local_inference_enabled

    try:
        from okuro.capability import capabilities

        detection = capabilities()
    except Exception:
        detection = {"gpus": []}

    if not local_inference_enabled(detection):
        return f"model-suggestions: skipped — edition '{detect_edition(detection)}' has no local inference"

    sugs = suggest_models(effective_detection(detection), _profile_goals(),
                          modality=modality, k=k, sort=sort)
    created = publish(sugs)
    return f"model-suggestions ({sort}): researched {len(sugs)}, filed {len(created)} new"


def _known_refs() -> set:
    """catalog_ids already suggested (open signals) or acquired (bundles)."""
    refs: set = set()
    try:
        from okuro.sense.signals import signal_list

        for sig in signal_list(status="open", limit=200):
            if sig.get("source") == "proactive" and sig.get("source_ref"):
                refs.add(sig["source_ref"])
    except Exception:
        pass
    try:
        from . import list_models

        for m in list_models():
            cid = m.get("catalog_id") or (m.get("source_ref") or {}).get("catalog_id")
            if cid:
                refs.add(cid)
    except Exception:
        pass
    return refs
