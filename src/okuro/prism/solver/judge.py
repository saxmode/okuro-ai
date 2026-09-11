# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 item 5 — the VISION JUDGE. Model + rubric specified here;
#   the judge looks at the RENDERED PIXELS (two slide screenshots) and picks the
#   A/B winner. It NEVER scores geometry proxies — proxy-metric judging is the
#   documented prism failure (memory f111f456 / 3390b6cd). If no vision-capable
#   provider answers, it returns an explicit needs-human-eye verdict and defers to
#   the orchestrator/USER eye (the brief's gate-(d) chain), rather than faking one.
# index:
#   RUBRIC / Verdict / build_prompt / judge_ab / probe_vision
# AGENT_HEADER_END -->
"""The vision A/B judge (v4.1 B6).

Model: a vision-capable Claude via the okuro bridge (the `claude` CLI reads the
two PNG paths). Rubric: six dimensions grounded in the kit design language
(gallery/design-language.html: uniformity, airiness, type hierarchy) and the
solver's own research score terms (reading gravity, focal dominance, grid
alignment, family character). The judge is handed ONLY the rendered images plus
the intended level/family, returns strict JSON, and picks a winner. Quality lives
in the pixels, so the judge lives in the pixels — the schema validators already
guaranteed structure; this adjudicates taste.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Rubric dimensions — each grounded in a source the reader can check (DP13).
RUBRIC: dict[str, str] = {
    "readability": "Can the eye find the one point in <3s? Clear focal, no wall of "
                   "text. (design-language.html airiness; efficient-web-layouts.md scanning)",
    "hierarchy": "Is there one dominant element that out-weighs the rest, with a "
                 "clean primary/supporting order? (score term focal_dominance; typographic §1 Ruder contrast)",
    "airiness": "Is whitespace active and sufficient — breathing room, not cramped, "
                "not cavernous? (design-language.html; score term whitespace; JP Ma)",
    "alignment": "Do elements sit on a shared grid/baseline — nothing off-axis? "
                 "(score term alignment; Swiss modular grid)",
    "family_character": "Does the composition read as its intended family "
                        "(swiss calm-modular / japanese airy-asymmetric / bauhaus kinetic / editorial column)?",
    "beauty": "Overall: would this pass as a hand-authored board slide? "
              "(the gold-standard reference eye)",
}

_SYSTEM = (
    "You are a senior presentation-design judge. You compare two rendered slide "
    "images (A and B) of the SAME content and pick which is the better LAYOUT. "
    "Judge only what you SEE in the pixels. Output STRICT JSON, no prose."
)

# Content-neutral SPATIAL rubric — the layout-only sub-score (brief item 3). Only
# geometry dimensions; the prompt hard-forbids letting text quality leak in. This
# de-confounds the full A/B (where the references carry REAL copy and the
# solver carries placeholder 'Item/feat/-92%' fill — an unfair content gap).
LAYOUT_RUBRIC: dict[str, str] = {
    "balance": "Notan mass balance — is visual weight distributed with intent "
               "(centred for calm families, deliberately off-centre for asymmetric), "
               "not accidentally lopsided or all clustered in one corner?",
    "alignment": "Do blocks share a grid — consistent column edges, gutters and "
                 "baselines, nothing off-axis or ragged?",
    "void": "Is empty space active and well-placed — breathing room and margins "
            "that frame content, not a dead hole beside a boxed-in cluster, nor a "
            "cramped wall?",
    "hierarchy": "By SIZE and POSITION alone (ignore the words), is there a clear "
                 "dominant block and a legible primary->supporting order?",
}

_LAYOUT_SYSTEM = (
    "You are a senior layout/composition judge. You compare two rendered slides "
    "purely as SPATIAL COMPOSITIONS — grid, balance, whitespace, size hierarchy. "
    "CRITICAL: judge ONLY geometry. IGNORE all text: wording, copy quality, real "
    "vs placeholder labels, repeated dummy strings ('Item', 'feat', '-92%', 'value'), "
    "language, numbers, iconography and colour meaning. If one slide has polished "
    "prose and the other has placeholder fill, that MUST NOT affect any score — "
    "you are grading the arrangement of blocks on the canvas, nothing else. "
    "Output STRICT JSON, no prose."
)


@dataclass
class Verdict:
    winner: str                          # "A" | "B" | "tie" | "needs-human-eye"
    method: str                          # "vision-model" | "needs-human-eye"
    scores_a: dict[str, float] = field(default_factory=dict)
    scores_b: dict[str, float] = field(default_factory=dict)
    rationale: str = ""
    raw: str = ""

    @property
    def decided(self) -> bool:
        return self.winner in ("A", "B", "tie")


def build_prompt(a_png: Path, b_png: Path, level: str, family: str, context: str = "") -> str:
    dims = "\n".join(f"  - {k}: {v}" for k, v in RUBRIC.items())
    return (
        f"{_SYSTEM}\n\n"
        f"Read BOTH images, then score each 0-10 on every rubric dimension.\n"
        f"IMAGE A: {a_png}\nIMAGE B: {b_png}\n\n"
        f"Intended depth level: {level} · intended grid family: {family}.\n"
        f"{('Content context: ' + context) if context else ''}\n\n"
        f"RUBRIC:\n{dims}\n\n"
        f"Return ONLY this JSON:\n"
        '{"scores_a": {<dim>: <0-10>, ...}, "scores_b": {<dim>: <0-10>, ...}, '
        '"winner": "A"|"B"|"tie", "rationale": "<one sentence>"}'
    )


def _extract_json(text: str) -> dict | None:
    """Robust JSON extraction from a CLI model reply: try a ```json fence, then
    every balanced {...} candidate (longest first). CLI providers wrap JSON in
    prose/markdown, so a single greedy regex is not enough."""
    if not text:
        return None
    candidates: list[str] = []
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    # all top-level {...} spans, longest first (the verdict object is the biggest)
    spans = re.findall(r"\{.*?\}", text, re.DOTALL) + re.findall(r"\{.*\}", text, re.DOTALL)
    candidates.extend(sorted(set(spans), key=len, reverse=True))
    for c in candidates:
        try:
            obj = json.loads(c)
            # accept an A/B verdict (has "winner") OR an absolute score object (the
            # L1 sparse-hook rubric returns {"scores": ...} with no winner).
            if isinstance(obj, dict) and ("winner" in obj or "scores" in obj):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def judge_ab(
    a_png: Path, b_png: Path, level: str, family: str, context: str = "",
    capability: str = "quality", timeout: int = 120,
) -> Verdict:
    """Judge two rendered slides. Returns a Verdict; method=needs-human-eye when
    no vision-capable provider answers (never a fabricated decision)."""
    prompt = build_prompt(Path(a_png).resolve(), Path(b_png).resolve(), level, family, context)
    try:
        from okuro.bridge.invoke import invoke
    except Exception as exc:  # bridge unavailable
        return Verdict("needs-human-eye", "needs-human-eye",
                       rationale=f"bridge import failed: {exc}")
    try:
        res = invoke(prompt=prompt, capability=capability, timeout=timeout)
    except Exception as exc:
        return Verdict("needs-human-eye", "needs-human-eye", rationale=f"invoke failed: {exc}")
    if not res or not res.get("success"):
        return Verdict("needs-human-eye", "needs-human-eye",
                       rationale=f"no provider answer: {(res or {}).get('error', 'unknown')}")
    out = res.get("output", "") or ""
    data = _extract_json(out)
    if not data or data.get("winner") not in ("A", "B", "tie"):
        return Verdict("needs-human-eye", "needs-human-eye", rationale="unparseable verdict", raw=out)
    return Verdict(
        winner=data["winner"], method="vision-model",
        scores_a={k: float(v) for k, v in (data.get("scores_a") or {}).items()},
        scores_b={k: float(v) for k, v in (data.get("scores_b") or {}).items()},
        rationale=str(data.get("rationale", "")), raw=out,
    )


def judge_vs_reference(
    solver_png: Path, reference_png: Path, level: str, family: str, context: str = "",
    **kw,
) -> Verdict:
    """Gate (d): solver output (as A) vs a hand-authored reference (as B). The
    solver 'wins/ties' iff winner in {A, tie} (solver scores >= reference)."""
    return judge_ab(solver_png, reference_png, level, family, context, **kw)


# R32 — the distinct-composition dimension, scored against the SHALLOWER level.
_DISTINCT_DIM = (
    "distinct_composition: does this level's MACRO-SKELETON differ from the shallower "
    "level shown as IMAGE C — a DIFFERENT dominant band / lead treatment, a re-composed "
    "layout — rather than the same slide with extra rows appended? (10 = clearly a "
    "distinct composition; 0 = C with rows added)")


def build_layout_prompt(a_png: Path, b_png: Path, level: str, family: str,
                        shallower_png: Path | None = None) -> str:
    dims_map = dict(LAYOUT_RUBRIC)
    extra = ""
    if shallower_png is not None:
        dims_map["distinct_composition"] = _DISTINCT_DIM.split(": ", 1)[1]
        extra = f"IMAGE C (the shallower level, for distinct_composition only): {shallower_png}\n"
    dims = "\n".join(f"  - {k}: {v}" for k, v in dims_map.items())
    return (
        f"{_LAYOUT_SYSTEM}\n\n"
        f"Read the images, then score each 0-10 on every SPATIAL dimension.\n"
        f"IMAGE A (solver): {a_png}\nIMAGE B (reference): {b_png}\n{extra}\n"
        f"Intended depth level: {level} · intended grid family: {family}.\n"
        f"Remember: geometry only, ignore every word and label.\n\n"
        f"SPATIAL RUBRIC:\n{dims}\n\n"
        f"Return ONLY this JSON:\n"
        '{"scores_a": {<dim>: <0-10>, ...}, "scores_b": {<dim>: <0-10>, ...}, '
        '"winner": "A"|"B"|"tie", "rationale": "<one sentence, geometry only>"}'
    )


def judge_layout_only(
    solver_png: Path, reference_png: Path, level: str, family: str,
    capability: str = "quality", timeout: int = 120, shallower_png: Path | None = None,
) -> Verdict:
    """Content-neutral SPATIAL sub-score (brief item 3): solver (A) vs a hand-
    authored reference (B), scored on LAYOUT_RUBRIC only, with the model instructed
    to ignore all text/content. When ``shallower_png`` is given, the R32 distinct-
    composition dimension is scored against it. needs-human-eye when no vision
    provider answers (never fabricated)."""
    prompt = build_layout_prompt(
        Path(solver_png).resolve(), Path(reference_png).resolve(), level, family,
        Path(shallower_png).resolve() if shallower_png else None,
    )
    try:
        from okuro.bridge.invoke import invoke
    except Exception as exc:
        return Verdict("needs-human-eye", "needs-human-eye", rationale=f"bridge import failed: {exc}")
    try:
        res = invoke(prompt=prompt, capability=capability, timeout=timeout)
    except Exception as exc:
        return Verdict("needs-human-eye", "needs-human-eye", rationale=f"invoke failed: {exc}")
    if not res or not res.get("success"):
        return Verdict("needs-human-eye", "needs-human-eye",
                       rationale=f"no provider answer: {(res or {}).get('error', 'unknown')}")
    data = _extract_json(res.get("output", "") or "")
    if not data or data.get("winner") not in ("A", "B", "tie"):
        return Verdict("needs-human-eye", "needs-human-eye", rationale="unparseable verdict",
                       raw=res.get("output", "") or "")
    return Verdict(
        winner=data["winner"], method="vision-model",
        scores_a={k: float(v) for k, v in (data.get("scores_a") or {}).items()},
        scores_b={k: float(v) for k, v in (data.get("scores_b") or {}).items()},
        rationale=str(data.get("rationale", "")), raw=res.get("output", "") or "",
    )


# ── W3: per-level rubrics (R31 density bars, level-matched) ────────────────────
# The vision judge MUST be level-matched (R31): judging an L1 hook against a dense
# reference slide is a category error. Each level gets its own rubric and
# its own comparison mode:
#   L1  — SPARSE HOOK, scored ABSOLUTELY (no dense reference). One idea, one strong
#         focal, generous whitespace, 3-5 key points — a wall of text is a FAILURE.
#   L2  — reference-density MATCH: the refs are L2-density comparators, so
#         L2 is judged vs a reference on the content-neutral LAYOUT_RUBRIC.
#   L3  — DENSEST: the reference is a density FLOOR, not a match (REC-1). L3 should
#         read as MORE information than the ref while staying organised — density
#         that is legible is rewarded, not penalised.
L1_HOOK_RUBRIC: dict[str, str] = {
    "one_idea": "Is there a single unmistakable focal idea graspable in <3s — one "
                "headline that dominates, not competing messages?",
    "restraint": "Is this SPARSE — a hook, not a wall? 3-5 key points at most; "
                 "generous whitespace. Cramming detail here is a FAILURE, not density.",
    "focal_strength": "Does one visual/headline clearly out-weigh everything else "
                      "by size and position (a commanding hook)?",
    "airiness": "Is whitespace generous and active — the calm of an opening slide, "
                "not a dense working slide?",
}
L3_DENSE_RUBRIC: dict[str, str] = {
    "information_density": "Does this read as the DENSEST level — more information "
                          "present than a typical board slide? Here MORE is better, "
                          "provided it stays legible (the reference is a FLOOR).",
    "organization": "Despite the density, is it organised — clear regions, grid "
                    "alignment, a legible reading order — not a chaotic dump?",
    "no_dead_void": "Is the space USED — no large dead holes beside dense clusters "
                    "(dense levels should fill the canvas purposefully)?",
    "hierarchy": "By size/position, is there still a dominant block and a "
                 "primary->supporting order even at high density?",
}
_L1_SYSTEM = (
    "You are a senior presentation-design judge scoring a single HOOK slide (the "
    "opening, lowest-resolution level of a topic). Judge it on its OWN terms as a "
    "sparse hook — do NOT expect or reward dense detail. A wall of text scores LOW. "
    "Judge only what you SEE. Output STRICT JSON, no prose."
)
_L3_SYSTEM = (
    "You are a senior layout/composition judge scoring the DENSEST level of a topic "
    "against a hand-authored reference. The reference is a DENSITY FLOOR: the solver "
    "slide SHOULD carry at least as much information, and MORE legible density is "
    "BETTER, not worse. Judge geometry + organisation, IGNORE wording/placeholder "
    "text quality. Output STRICT JSON, no prose."
)


def build_l1_prompt(a_png: Path, family: str) -> str:
    dims = "\n".join(f"  - {k}: {v}" for k, v in L1_HOOK_RUBRIC.items())
    return (
        f"{_L1_SYSTEM}\n\nRead the image, then score each 0-10 on every dimension.\n"
        f"IMAGE: {a_png}\n\nIntended: L1 hook · grid family {family}.\n\n"
        f"HOOK RUBRIC:\n{dims}\n\n"
        f"Return ONLY this JSON:\n"
        '{"scores": {<dim>: <0-10>, ...}, "rationale": "<one sentence>"}'
    )


def build_l3_prompt(a_png: Path, b_png: Path, family: str,
                    shallower_png: Path | None = None) -> str:
    dims_map = dict(L3_DENSE_RUBRIC)
    extra = ""
    if shallower_png is not None:
        dims_map["distinct_composition"] = _DISTINCT_DIM.split(": ", 1)[1]
        extra = f"IMAGE C (the shallower level, for distinct_composition only): {shallower_png}\n"
    dims = "\n".join(f"  - {k}: {v}" for k, v in dims_map.items())
    return (
        f"{_L3_SYSTEM}\n\nRead the images, then score each 0-10 on every dimension.\n"
        f"IMAGE A (solver, densest level): {a_png}\nIMAGE B (reference, density floor): {b_png}\n{extra}\n"
        f"Intended: L3 densest · grid family {family}. Remember: the reference is a "
        f"FLOOR — more legible density in A is better.\n\n"
        f"DENSE RUBRIC:\n{dims}\n\n"
        f"Return ONLY this JSON:\n"
        '{"scores_a": {<dim>: <0-10>, ...}, "scores_b": {<dim>: <0-10>, ...}, '
        '"winner": "A"|"B"|"tie", "rationale": "<one sentence>"}'
    )


def _invoke_json(prompt: str, capability: str, timeout: int) -> tuple[dict | None, str]:
    """Shared bridge call -> (parsed_json_or_None, raw_output). Never fabricates."""
    try:
        from okuro.bridge.invoke import invoke
    except Exception as exc:
        return None, f"bridge import failed: {exc}"
    try:
        res = invoke(prompt=prompt, capability=capability, timeout=timeout)
    except Exception as exc:
        return None, f"invoke failed: {exc}"
    if not res or not res.get("success"):
        return None, f"no provider answer: {(res or {}).get('error', 'unknown')}"
    out = res.get("output", "") or ""
    return _extract_json(out), out


def judge_level(
    solver_png: Path, level: str, family: str, reference_png: Path | None = None,
    capability: str = "quality", timeout: int = 120, retries: int = 1,
    shallower_png: Path | None = None,
) -> Verdict:
    """Level-matched vision judge (R31). L1 -> absolute sparse-hook score (no
    reference); L2 -> content-neutral layout match vs reference; L3 -> dense rubric
    with the reference as a FLOOR. ``shallower_png`` (the level below) adds the R32
    distinct-composition dimension. needs-human-eye when no vision provider answers
    (never fabricated). L1/L3 verdicts carry the rubric scores in ``scores_a``.
    Retries once on an unparseable reply — the CLI vision model intermittently wraps
    or truncates its JSON; a bounded retry self-heals without fabricating."""
    for _ in range(max(1, retries + 1)):
        v = _judge_level_once(solver_png, level, family, reference_png, capability,
                              timeout, shallower_png)
        if v.method == "vision-model":
            return v
    return v


def _judge_level_once(
    solver_png: Path, level: str, family: str, reference_png: Path | None,
    capability: str, timeout: int, shallower_png: Path | None = None,
) -> Verdict:
    lvl = level.upper()
    if lvl == "L1":
        data, raw = _invoke_json(build_l1_prompt(Path(solver_png).resolve(), family),
                                 capability, timeout)
        scores = (data or {}).get("scores") or (data or {}).get("scores_a")
        if not data or not scores:
            return Verdict("needs-human-eye", "needs-human-eye",
                           rationale="unparseable L1 verdict", raw=raw)
        return Verdict(
            winner="A", method="vision-model",
            scores_a={k: float(v) for k, v in scores.items()},
            rationale=str(data.get("rationale", "")), raw=raw,
        )
    if reference_png is None:
        return Verdict("needs-human-eye", "needs-human-eye",
                       rationale=f"{lvl} needs a reference for level-matched judging")
    if lvl == "L3":
        data, raw = _invoke_json(
            build_l3_prompt(Path(solver_png).resolve(), Path(reference_png).resolve(), family,
                            Path(shallower_png).resolve() if shallower_png else None),
            capability, timeout)
        if not data or data.get("winner") not in ("A", "B", "tie"):
            return Verdict("needs-human-eye", "needs-human-eye",
                           rationale="unparseable L3 verdict", raw=raw)
        return Verdict(
            winner=data["winner"], method="vision-model",
            scores_a={k: float(v) for k, v in (data.get("scores_a") or {}).items()},
            scores_b={k: float(v) for k, v in (data.get("scores_b") or {}).items()},
            rationale=str(data.get("rationale", "")), raw=raw,
        )
    # L2 (and any other): content-neutral layout match vs the reference.
    return judge_layout_only(solver_png, reference_png, lvl, family, capability, timeout,
                             shallower_png=shallower_png)


# ── W5 Phase D: the WHOLE-DECK judge (falsifiable — pre-check blocking catch #1) ─
# The per-slide level judges score cells; this scores the DECK as an artifact:
# narrative arc, cross-topic variety, hero + index quality, and the head-to-head vs
# the hand-authored reference. Every dimension has a MIN score and the gate is
# "every dim >= threshold AND mean >= threshold" — NOT "reported" (the exact defect
# that FAILed W1/W3 and that the W5 pre-check blocked). needs-human-eye when no
# vision provider answers (never a fabricated pass).
WHOLE_DECK_RUBRIC: dict[str, str] = {
    "arc_coherence": "Do the topics read as ONE intended throughline (a board "
                     "narrative that builds), not a pile of unrelated slides?",
    "cross_topic_variety": "Across topics, do the compositions VARY — different "
                           "dominant treatments, not the same layout repeated?",
    "hero_quality": "Is the hero/cover a commanding opening — clear title, orienting "
                    "personas/meta, board-grade first impression?",
    "index_quality": "Is the index/overview a legible map of the whole deck — one "
                     "can see the topics × depth structure at a glance?",
    "overall_vs_manual": "Head-to-head with the hand-authored reference deck (IMAGE "
                         "REF): does this deck read as at least as good — as clear, "
                         "as varied, as board-worthy? (>=7 means it holds its own or better)",
}
_WHOLE_DECK_SYSTEM = (
    "You are a senior board-presentation critic judging a WHOLE deck (not a single "
    "slide) against a hand-authored reference deck. You are given the deck's hero, "
    "its index/overview, and one full topic column (L1->L4), plus the reference "
    "deck's cover/index. Judge the deck as an artifact: arc, variety, hero, index, "
    "and how it stacks up against the reference. Output STRICT JSON, no prose."
)


@dataclass
class WholeDeckVerdict:
    scores: dict[str, float] = field(default_factory=dict)
    method: str = "vision-model"          # "vision-model" | "needs-human-eye"
    rationale: str = ""
    raw: str = ""
    threshold: float = 7.0

    @property
    def mean(self) -> float:
        vals = [v for v in self.scores.values() if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    @property
    def min_dim(self) -> float:
        vals = [v for v in self.scores.values() if isinstance(v, (int, float))]
        return min(vals) if vals else 0.0

    @property
    def passed(self) -> bool:
        """Falsifiable gate: EVERY dimension >= threshold AND the mean >= threshold.
        A missing verdict (needs-human-eye) never passes."""
        return (self.method == "vision-model" and bool(self.scores)
                and self.min_dim >= self.threshold and self.mean >= self.threshold)

    def summary(self) -> str:
        dims = " · ".join(f"{k} {v}" for k, v in self.scores.items())
        return (f"whole-deck {'PASS' if self.passed else 'FAIL'} "
                f"(mean {self.mean} · min {self.min_dim} · thr {self.threshold}) — {dims}")


def build_whole_deck_prompt(hero_png: Path, index_png: Path, topic_pngs: list[Path],
                            reference_png: Path | None) -> str:
    dims_map = dict(WHOLE_DECK_RUBRIC)
    if reference_png is None:
        dims_map.pop("overall_vs_manual", None)
    dims = "\n".join(f"  - {k}: {v}" for k, v in dims_map.items())
    topic_lines = "\n".join(f"IMAGE TOPIC-{i + 1} (a full topic column L1->L4): {p}"
                            for i, p in enumerate(topic_pngs))
    ref = f"IMAGE REF (the hand-authored reference deck cover/index): {reference_png}\n" if reference_png else ""
    return (
        f"{_WHOLE_DECK_SYSTEM}\n\n"
        f"Read ALL images, then score each 0-10 on every dimension.\n"
        f"IMAGE HERO (the deck cover): {hero_png}\n"
        f"IMAGE INDEX (the deck overview/grid): {index_png}\n"
        f"{topic_lines}\n{ref}\n"
        f"RUBRIC:\n{dims}\n\n"
        f"Return ONLY this JSON:\n"
        '{"scores": {<dim>: <0-10>, ...}, "rationale": "<one or two sentences>"}'
    )


def judge_whole_deck(
    hero_png: Path, index_png: Path, topic_pngs: list[Path],
    reference_png: Path | None = None, capability: str = "quality",
    timeout: int = 240, threshold: float = 7.0, retries: int = 1,
) -> WholeDeckVerdict:
    """Score the deck as a whole against a hand-authored reference. Falsifiable:
    ``verdict.passed`` iff every dim >= threshold and the mean >= threshold.
    needs-human-eye when no vision provider answers (never fabricated)."""
    prompt = build_whole_deck_prompt(
        Path(hero_png).resolve(), Path(index_png).resolve(),
        [Path(p).resolve() for p in topic_pngs],
        Path(reference_png).resolve() if reference_png else None,
    )
    data = raw = None
    for _ in range(max(1, retries + 1)):
        data, raw = _invoke_json(prompt, capability, timeout)
        if data and (data.get("scores") or {}):
            break
    scores = (data or {}).get("scores") or {}
    if not scores:
        return WholeDeckVerdict(method="needs-human-eye",
                                rationale="no whole-deck verdict from a vision provider",
                                raw=raw or "", threshold=threshold)
    return WholeDeckVerdict(
        scores={k: float(v) for k, v in scores.items() if isinstance(v, (int, float))},
        method="vision-model", rationale=str((data or {}).get("rationale", "")),
        raw=raw or "", threshold=threshold,
    )


def probe_vision(sample_png: Path) -> bool:
    """Best-effort check whether the configured bridge can actually SEE an image
    (reads the PNG and reports a visible detail). Cheap 1-shot; False = degrade."""
    try:
        from okuro.bridge.invoke import invoke
        res = invoke(
            prompt=(f"Read the image at {Path(sample_png).resolve()} and reply with ONLY "
                    f'JSON {{"saw_image": true|false, "kind": "<one word>"}}.'),
            capability="quality", timeout=180,   # CLI cold-start can exceed 90s
        )
        data = _extract_json(res.get("output", "") or "") if res.get("success") else None
        return bool(data and data.get("saw_image") is True)
    except Exception:
        return False


__all__ = [
    "RUBRIC", "LAYOUT_RUBRIC", "L1_HOOK_RUBRIC", "L3_DENSE_RUBRIC",
    "Verdict", "build_prompt", "build_layout_prompt",
    "build_l1_prompt", "build_l3_prompt",
    "judge_ab", "judge_vs_reference", "judge_layout_only", "judge_level",
    "WHOLE_DECK_RUBRIC", "WholeDeckVerdict", "build_whole_deck_prompt", "judge_whole_deck",
    "probe_vision",
]
