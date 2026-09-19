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
#   def installed_by_modality | def judge_interesting | def enrich   (P4)
#   def scan_category        (P4 — one wanted bucket, with its zero-result flag)
#   def run_suggestions      (P4 — every category, LLM budget capped)
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
    # --- P4: which bucket, what line, and is it worth a look ----------------
    category: Optional[str] = None
    family: Optional[str] = None
    version: Optional[str] = None
    params_total_b: Optional[float] = None
    params_active_b: Optional[float] = None
    quant: Optional[str] = None
    variant_tags: list[str] = field(default_factory=list)
    relation: Optional[str] = None
    relation_target: Optional[str] = None
    relation_confidence: Optional[float] = None
    best_placement: Optional[str] = None
    runnable_idle: bool = False
    interesting: Optional[bool] = None
    interesting_why: str = ""
    fit_result: dict = field(default_factory=dict)
    #: True only when `identical` is EVIDENCED, not merely undisputed — see
    #: :func:`proves_same_file`. This, not the relation alone, is what marks a
    #: candidate installed.
    same_file_as_installed: bool = False

    @property
    def identity_variants(self) -> list[str]:
        """The tags the Discover feed hides behind the `variants` toggle.

        Tolerates a None — a caller outside the scan (the proactive engine's
        own fixtures, a hand-built suggestion) may not set the field, and an
        absent tag list is an empty one, never an error.
        """
        from .lineage import IDENTITY_VARIANT_TAGS

        return [t for t in (self.variant_tags or []) if t in IDENTITY_VARIANT_TAGS]

    def relation_badge(self) -> str:
        """One short phrase for a list row, a CLI cell or the morning brief.

        Empty when the lineage pass has not judged this row — which is a
        different fact from `unrelated`, and reads as such.
        """
        if not self.relation or self.relation == "unrelated":
            return ""
        target = (self.relation_target or "").split(":", 1)[-1].split("/")[-1]
        return f"{self.relation} {target}".strip() if target else self.relation

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
            "category": self.category,
            "variant_tags": list(self.variant_tags or []),
            "relation": self.relation,
            "relation_target": self.relation_target,
            "best_placement": self.best_placement,
            "runnable_idle": self.runnable_idle,
            "interesting": self.interesting,
            "interesting_why": self.interesting_why,
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


# --- P4: lineage, fit, and what "might be interesting" means ----------------

#: Relations that make a candidate worth the user's attention. `identical` is
#: deliberately absent — that is the model already on disk, and offering it is
#: the exact failure the old exact-string dedup produced from the other side.
#: `same-family-older` is absent for the same reason in reverse.
INTERESTING_RELATIONS = frozenset({
    "same-family-newer",
    "variant-of",
    "same-family-same-version-other-quant",
})


def installed_by_modality() -> dict[str, int]:
    """Installed unit count per modality — how a category is known to be empty.

    A candidate in a category with nothing installed is interesting WITHOUT a
    relation, because you cannot have a newer version of something you do not
    have. Without this the 3d and music buckets could never produce a
    suggestion however good the release.

    Returns ``{}`` when no store is configured, which reads as "nothing is
    known to be installed" and is the honest answer on a host with no stores.
    """
    counts: dict[str, int] = {}
    try:
        from okuro.db import get_db

        from . import fitting

        rows = get_db().fetchall(
            "SELECT family, rel_path FROM model_units WHERE status != 'missing'")
    except Exception:
        return {}
    for r in rows:
        try:
            m = fitting.unit_modality(dict(r))
        except Exception:
            continue
        counts[m] = counts.get(m, 0) + 1
    return counts


def _relation_for(entry, *, relate_fn: Optional[Callable] = None) -> dict:
    """Best relation between a candidate and what is installed.

    Never raises: a candidate with an unparseable name, or a host with no
    inventory, yields ``{}`` — and NULL relation means "not asked", which is a
    different row from `unrelated`.
    """
    if relate_fn is None:
        from .lineage import relate_release as relate_fn
    try:
        res = relate_fn(entry.display_name or entry.catalog_id)
    except Exception:
        return {}
    out = {"candidate": res.get("candidate") or {}}
    rels = res.get("relations") or []
    if rels:
        # THE DEDUP QUESTION AND THE DISPLAY QUESTION ARE NOT THE SAME QUESTION,
        # and asking only rels[0] answers the wrong one. P3 sorts
        # `same-family-newer` above `identical` because that is the right order
        # for a badge. MEASURED on this host: a release named
        # `Llama-3.3-70B-Instruct.Q4_K_M` returns a proven identical against the
        # exact file on disk at confidence 0.85 — at index [1], masked by a
        # same-family-newer at 0.64 against an unrelated DeepSeek distill. Taken
        # from rels[0] alone, a model already on disk is published as new.
        #
        # So the whole list is scanned for an EVIDENCED identical, and when one
        # exists it is what gets reported: "you already have this file" is the
        # strongest thing that can be said about a candidate.
        quant = (out["candidate"] or {}).get("quant")
        proven = next(
            (r for r in rels
             if r.get("relation") == "identical"
             and proves_same_file(quant, (r.get("unit") or {}).get("quant"))),
            None)
        top = proven or rels[0]
        out.update({
            "relation": top.get("relation"),
            "relation_target": (top.get("unit") or {}).get("unit_id"),
            "relation_confidence": top.get("confidence"),
            "relation_why": top.get("why"),
            "relation_unit_quant": (top.get("unit") or {}).get("quant"),
            "same_file": proven is not None,
        })
    elif out["candidate"].get("family"):
        # Family parsed, no installed unit of that line. That is a FACT, not a
        # missing answer, and it is exactly what makes an empty line
        # interesting — so it is recorded as `unrelated`, not left NULL.
        out["relation"] = "unrelated"
        out["relation_why"] = "no installed unit of this family"
    return out


def _fit_for(entry, *, fit_fn: Optional[Callable] = None) -> dict:
    """Placement space for a candidate. Nothing is downloaded or opened."""
    if fit_fn is None:
        from .fitting import fit_release as fit_fn
    try:
        best = entry.best_runnable_format
        return fit_fn(
            {"catalog_id": entry.catalog_id,
             "display_name": entry.display_name or entry.catalog_id,
             "modality": entry.modality,
             "size_gb": getattr(best, "size_gb", None)},
            size_gb=getattr(best, "size_gb", None),
        )
    except Exception:
        return {}


def proves_same_file(candidate_quant, unit_quant) -> bool:
    """Is an ``identical`` verdict EVIDENCE of sameness, or absence of difference?

    For two installed UNITS the difference is settled by content — P1 fingerprints
    every unit, and all ten of this host's cross-store twin groups are genuinely
    byte-identical. For a RELEASE there is no content to compare, because nothing
    has been downloaded. So the relation falls through to ``identical`` whenever
    it can find nothing that differs, and its own ``why`` says so: "neither name
    states a quant".

    MEASURED on the real feed 2026-09-15: 4 of 30 published candidates came back
    ``identical`` and all four were wrong — a V5 checkpoint against the installed
    V4, a bf16 repo against a Q8_0 file, a 4B model against a decoder COMPONENT
    of itself, and a v1.5 release against the installed v1. Marking those
    installed buries a genuine discovery, which is the exact failure the
    exact-catalog_id dedup produced from the other side.

    Confidence cannot separate the two: an assumed identical scores 0.51 and so
    does a real same-family-newer. What separates them is whether the compare was
    DECIDED — both sides naming the same quant — or DEFAULTED.
    """
    if not candidate_quant or not unit_quant:
        return False
    return str(candidate_quant).upper() == str(unit_quant).upper()


def judge_interesting(s: ModelSuggestion, installed: dict[str, int]) -> tuple[bool, str]:
    """"Might be interesting for you" — computed, never guessed.

    Runnable when idle AND (it relates to something installed, or its category
    has nothing installed at all). Runnability comes first because a model this
    box cannot run is not interesting however novel it is — and `runnable_idle`
    is the OR over the whole placement space, so a model that fits no single
    card still counts when it fits split or offloaded.
    """
    if not s.runnable_idle:
        return False, "nothing on this host can run it, even idle"
    if s.relation == "identical" and not s.same_file_as_installed:
        # The relation found nothing that differs, which is not the same as
        # proving sameness. Fall through to the category rule rather than
        # dismissing a candidate on an assumption.
        if installed.get(s.modality, 0) == 0:
            return True, f"nothing installed in the {s.modality} category yet"
        return False, ("the same line as an installed unit, but neither name "
                       "states a quant — not proven to be the same file")
    if s.relation in INTERESTING_RELATIONS:
        target = (s.relation_target or "").split(":", 1)[-1]
        return True, f"{s.relation} of installed {target}" if target else s.relation
    if installed.get(s.modality, 0) == 0:
        return True, f"nothing installed in the {s.modality} category yet"
    return False, (
        f"runs here, but unrelated to any of the {installed.get(s.modality, 0)} "
        f"installed {s.modality} units")


def enrich(s: ModelSuggestion, entry, installed: dict[str, int], *,
           relate_fn: Optional[Callable] = None,
           fit_fn: Optional[Callable] = None) -> ModelSuggestion:
    """Write lineage, placement and the interesting verdict onto a suggestion."""
    from . import catalog as _catalog

    s.variant_tags = _catalog.variant_tags(entry)

    rel = _relation_for(entry, relate_fn=relate_fn)
    cand = rel.get("candidate") or {}
    s.family = cand.get("family")
    s.version = cand.get("version")
    s.params_total_b = cand.get("params_total_b")
    s.params_active_b = cand.get("params_active_b")
    s.quant = cand.get("quant")
    # The parser sees the name; the source sees its own flags. Union, so a
    # Civitai row flagged nsfw keeps that tag even when the name is silent.
    for t in cand.get("variant_tags") or []:
        if t not in s.variant_tags:
            s.variant_tags.append(t)
    s.variant_tags.sort()
    s.relation = rel.get("relation")
    s.relation_target = rel.get("relation_target")
    s.relation_confidence = rel.get("relation_confidence")
    s.same_file_as_installed = bool(rel.get("same_file"))

    fit = _fit_for(entry, fit_fn=fit_fn)
    s.fit_result = fit or {}
    best = (fit or {}).get("best") or {}
    s.best_placement = best.get("mode")
    s.runnable_idle = bool((fit or {}).get("runnable_idle"))

    s.interesting, s.interesting_why = judge_interesting(s, installed)
    return s


def publish(
    suggestions: list[ModelSuggestion],
    *,
    existing_refs: Optional[set] = None,
    signal_add_fn: Optional[Callable] = None,
    upsert_fn: Optional[Callable] = None,
    mark_installed_fn: Optional[Callable] = None,
    persist_fits_fn: Optional[Callable] = None,
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
        # The placement space goes to the SAME table P3 stores unit fits in,
        # under subject_kind='release'. One table, one arithmetic, so the
        # Discover card and the unit card cannot disagree about what runs here.
        if s.fit_result.get("placements"):
            try:
                if persist_fits_fn is None:
                    from .fitting import persist_fits as persist_fits_fn
                persist_fits_fn("release", s.catalog_id, s.fit_result)
            except Exception:
                pass
        # SECOND DEDUP AXIS, and the one `_known_refs` structurally cannot see.
        # `_known_refs` compares catalog_id strings exactly, so a release and
        # the file of it already on disk — differently named by definition —
        # read as two different things. An EVIDENCED `identical` says they are
        # one thing. Marked installed, never notified.
        #
        # `same_file_as_installed`, not `relation == 'identical'`, because for a
        # release the relation falls through to identical whenever it can find
        # nothing that differs. See proves_same_file for the four real
        # candidates that rule buried on this host.
        if s.same_file_as_installed:
            if mark_installed_fn is None:
                from .discoveries import mark_installed_if_new as mark_installed_fn
            try:
                mark_installed_fn(s.catalog_id)
            except Exception:
                pass
            existing_refs.add(s.catalog_id)
            continue
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


def scan_category(
    cat: dict,
    detection: dict,
    *,
    sort: str = "trending",
    limit: int = 20,
    search_fn: Optional[Callable] = None,
) -> dict:
    """Fetch and gate ONE wanted category. No LLM, no database.

    Returns the ledger row plus the survivors, so the caller can record what a
    category found — including nothing — without re-deriving the counts.
    """
    from .discovery import search_category as _search

    fn = search_fn or _search
    try:
        entries = fn(cat, limit=limit, sort=sort) or []
    except Exception as exc:  # one dead source must not end the run
        return {"category": cat.get("name"), "source": cat.get("source", "hf"),
                "selector": _selector(cat), "fetched": 0, "passed_gate": 0,
                "entries": [], "zero_result": True,
                "note": f"source error: {exc}"[:200]}

    ranked = catalog.rank(entries, detection, modality=cat.get("modality") or None,
                          min_downloads=cat.get("min_downloads"))
    requires = {str(t).lower() for t in (cat.get("requires_tags") or [])}
    if requires:
        # Keeps a DEDICATED bucket on topic. Not the drop ruling 3 forbids:
        # the plain bucket for the same modality runs with no tag filter, so
        # nothing excluded here is lost from the feed as a whole.
        ranked = [e for e in ranked
                  if requires & {t.lower() for t in catalog.variant_tags(e)}]

    zero = not ranked
    note = None
    if zero:
        note = (f"no reputable release this week — {len(entries)} candidate(s) "
                f"fetched, none passed the gate"
                if entries else "no reputable release this week — source "
                                "returned nothing for this selector")
    return {
        "category": cat.get("name"), "source": cat.get("source", "hf"),
        "selector": _selector(cat), "fetched": len(entries),
        "passed_gate": len(ranked), "entries": ranked,
        "zero_result": zero, "note": note,
    }


def _selector(cat: dict) -> str:
    """One string naming what was asked of the source, for the ledger."""
    bits = []
    tag = cat.get("pipeline_tag")
    if tag:
        bits.append("+".join(tag) if isinstance(tag, (list, tuple)) else str(tag))
    if cat.get("types"):
        bits.append("types=" + ",".join(str(t) for t in cat["types"]))
    if cat.get("nsfw"):
        bits.append("nsfw=true")
    if cat.get("query"):
        bits.append(f"q={cat['query']}")
    return " ".join(bits)


#: LLM calls the weekly run may spend, across ALL categories. The research step
#: is the only paid part of the scan; everything else is arithmetic over free
#: HTTP. Ten categories at k=3 would be thirty calls a week for text the user
#: mostly skims, so the budget is small by default and the rationale degrades
#: to the deterministic purpose summary once it is spent — which is what the
#: `researched` flag on every row records.
DEFAULT_MAX_RESEARCH = 6


def run_suggestions(*, k: int = 3, sort: str = "trending",
                    max_research: Optional[int] = None,
                    only_category: Optional[str] = None,
                    search_fn: Optional[Callable] = None,
                    invoke_fn: Optional[Callable] = None) -> str:
    """Daemon entrypoint (okuro.ai_models.suggest:run_suggestions).

    Walks EVERY wanted category — text, text-abliterated, image, image-nsfw,
    video, video-nsfw, music, vision, 3d, embedding — rather than the single
    ``text`` modality it walked before P4. Per candidate it writes the lineage
    relation against the installed inventory and the placement fit, and from
    those two computes whether it "might be interesting": runnable when idle
    AND (related to something installed, or its category has nothing installed
    at all).

    ``sort='trending'`` is the "new AND greatly reviewed" feed; raw 'new' is a
    firehose of test uploads the quality gate rightly drops to zero.

    A category that yields nothing records a zero-result row saying so —
    silence is ambiguous between "nothing was released" and "this scanner is
    broken", and those need different actions.
    """
    from .discovery import categories
    from .edition import detect_edition, effective_detection, local_inference_enabled

    try:
        from okuro.capability import capabilities

        detection = capabilities()
    except Exception:
        detection = {"gpus": []}

    if not local_inference_enabled(detection):
        return (f"model-suggestions: skipped — edition "
                f"'{detect_edition(detection)}' has no local inference")

    eff = effective_detection(detection)
    goals = _profile_goals()
    installed = installed_by_modality()
    budget = DEFAULT_MAX_RESEARCH if max_research is None else int(max_research)

    cats = [c for c in categories()
            if not only_category or c.get("name") == only_category]
    claimed: set[str] = set()
    all_sugs: list[ModelSuggestion] = []
    ledger: list[dict] = []
    llm_calls = 0

    for cat in cats:
        result = scan_category(cat, eff, sort=sort, search_fn=search_fn)
        picks = [e for e in result["entries"] if e.catalog_id not in claimed][:k]
        sugs: list[ModelSuggestion] = []
        for entry in picks:
            claimed.add(entry.catalog_id)
            if budget > 0:
                researched = research(entry, goals, invoke_fn=invoke_fn)
                llm_calls += 1
                budget -= 1
            else:
                researched = {
                    "rationale": catalog.derive_purpose_summary(entry),
                    "use_case": entry.task.replace("-", " "),
                    "caveats": "auto-summary (research budget spent)",
                    "researched": False,
                }
            s = build_suggestion(entry, eff, researched)
            s.category = cat.get("name")
            enrich(s, entry, installed)
            sugs.append(s)

        all_sugs.extend(sugs)
        n_interesting = sum(1 for s in sugs if s.interesting)
        ledger.append({**{kk: result[kk] for kk in
                          ("category", "source", "selector", "fetched",
                           "passed_gate", "zero_result", "note")},
                       "published": len(sugs), "interesting": n_interesting})

    created = publish(all_sugs)
    for row in ledger:
        try:
            from .discoveries import record_category_scan

            record_category_scan(row)
        except Exception:
            pass

    empty = [r["category"] for r in ledger if r["zero_result"]]
    interesting = sum(1 for s in all_sugs if s.interesting)
    return (f"model-suggestions ({sort}): {len(cats)} categories, "
            f"{len(all_sugs)} candidates, {interesting} interesting, "
            f"{len(created)} new signal(s), {llm_calls} LLM call(s)"
            + (f", empty: {', '.join(empty)}" if empty else ""))


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
