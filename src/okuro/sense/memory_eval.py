# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Measure whether memory recall actually retrieves — MRR/recall@k
#   round-trip plus junk rejection. The gate the 2026-05→07 outage lacked.
# index: imports | query construction | run_eval | main
# AGENT_HEADER_END -->
"""Recall eval for agent memory.

Why this exists: between 2026-05-10 and 2026-07-15 read_memory returned the
same ten recency-ordered rows for EVERY query — sourdough and cortex got
identical answers — and the whole suite stayed green. Every memory test
asserted plumbing (rows came back) and none asserted retrieval (the RIGHT
rows came back). Two months passed. cortex had an eval harness the entire
time and caught its own version of this bug; memory had none.

Method — deliberately self-calibrating, no hand-maintained gold set:

  * ROUND-TRIP. Sample real memories, build a query from a slice of each
    one's own text, and ask where that memory ranks in the results. A store
    that retrieves cannot fail to find a memory using its own words. Under
    the metric defect this scores ~0, because the recency fallback surfaces
    whatever is newest rather than what was asked for.
  * JUNK REJECTION. Off-domain queries must return nothing. This is the half
    a gold set misses: the outage's signature was not "no answer", it was a
    confident WRONG answer assembled from unrelated rows.

A hand-written gold set was rejected: it rots against a live store, and
whoever writes it picks the queries — which is exactly how a first pass at
_SIM_FLOOR=0.62 looked like a clean separation while silently dropping 2 of
19 real matches. Sampling the store removes the author's thumb.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from typing import Optional

# Off-domain probes. Nothing in a personal-AI-OS store should match these;
# any hit is the fallback path serving unrelated rows as if they were recall.
JUNK_QUERIES = [
    "how do I bake sourdough bread",
    "who won the 1998 football world cup",
    "best hiking boots for winter walking",
    "recipe for chocolate chip cookies",
    "what is the capital of mongolia",
    "how to tune a violin by ear",
    "symptoms of vitamin d deficiency",
    "when did the roman empire fall",
]

_WORD_RE = re.compile(r"[A-Za-z0-9_.:/-]+")


def _query_from(content: str, rng: random.Random) -> Optional[str]:
    """Build a plausible 'I half-remember this' query from a memory's text.

    A MIDDLE slice, not the opening words: a prefix is close to an exact
    substring and would flatter the score. Twelve words is long enough to
    carry the topic and short enough to look like something an agent types.
    """
    words = _WORD_RE.findall(content or "")
    if len(words) < 14:
        return None
    span = 12
    # Skip the first few words (often a topic tag like "[gotcha]") and pick a
    # window deterministically from the seeded rng.
    lo = min(3, max(0, len(words) - span))
    hi = max(lo, len(words) - span)
    start = rng.randint(lo, hi)
    return " ".join(words[start:start + span])


def run_eval(sample: int = 40, k: int = 10, seed: int = 7) -> dict:
    """Round-trip retrieval + junk rejection over the live memory store.

    AUTHORITATIVE: measures the SHIPPED read path, which is HYBRID
    (dense vector arm + BM25 lexical arm, RRF-fused — see okuro.sense.memory
    _lexical_ids/_rrf_fuse). The top-level keys (``mrr``, ``recall_at_{k}``,
    ``top1``, ``junk_rejection``) are the hybrid numbers agents actually get,
    so the gate certifies what the system runs — not a convention it doesn't.

    The pure-VECTOR arm is also reported (``vector_*``) as the diagnostic for
    retuning ``_SIM_FLOOR``: the floor only governs the vector arm, so retune
    against ``vector_recall_at_{k}`` / ``vector_junk_rejection``, then confirm
    the hybrid numbers hold. Junk safety is inherited — the lexical arm fires
    only when the floored vector arm is non-empty (the same gate as the read
    path), so ``junk_rejection`` == ``vector_junk_rejection`` by construction.

    Deterministic for a given (sample, k, seed) and store — no writes.
    """
    from okuro.db import get_db
    from okuro.embed.client import embed_query, to_bytes
    from okuro.sense.memory import (
        _QUERY_INSTRUCTION, _SIM_FLOOR, _lexical_ids, _rrf_fuse,
    )

    # Mirror read_memory's query embedding EXACTLY — same asymmetric wrapper,
    # same instruction. If the eval embedded queries differently from the read
    # path, the gate would certify a convention the system does not use.
    def _q(text: str) -> bytes:
        return to_bytes(embed_query(text, instruction=_QUERY_INSTRUCTION))

    db = get_db()
    rng = random.Random(seed)

    rows = db.fetchall(
        "SELECT id, content FROM agent_memory WHERE content IS NOT NULL"
    )
    pool = [r for r in rows if len(_WORD_RE.findall(r["content"])) >= 14]
    rng.shuffle(pool)
    picked = pool[:sample]

    def _vector_ids(qtext: str) -> list[str]:
        # Mirror read_memory: widen the pool to limit*4, floor, keep order.
        hits = db.vec_search("vec_memory", _q(qtext), limit=k * 4)
        return [h["id"] for h in hits if (1 - h["distance"]) > _SIM_FLOOR]

    def _hybrid_ids(qtext: str, vec_ids: list[str]) -> list[str]:
        # Gate: the lexical arm only fires behind a non-empty vector arm, so
        # off-domain junk (empty vector arm) stays rejected by the same floor.
        if not vec_ids:
            return []
        return _rrf_fuse(vec_ids, _lexical_ids(db, qtext, k * 4))

    vec_ranks: list[Optional[int]] = []
    hyb_ranks: list[Optional[int]] = []
    for r in picked:
        q = _query_from(r["content"], rng)
        if not q:
            continue
        vec_ids = _vector_ids(q)
        hyb_ids = _hybrid_ids(q, vec_ids)
        vec_ranks.append(next((i + 1 for i, x in enumerate(vec_ids[:k]) if x == r["id"]), None))
        hyb_ranks.append(next((i + 1 for i, x in enumerate(hyb_ids[:k]) if x == r["id"]), None))

    def _metrics(ranks: list[Optional[int]]) -> tuple[float, float, float]:
        n = len(ranks) or 1
        return (sum(1.0 / rk for rk in ranks if rk) / n,          # mrr
                sum(1 for rk in ranks if rk) / n,                 # recall@k
                sum(1 for rk in ranks if rk == 1) / n)            # top1

    v_mrr, v_rec, v_top1 = _metrics(vec_ranks)
    h_mrr, h_rec, h_top1 = _metrics(hyb_ranks)

    def _junk_admitted(hybrid: bool) -> int:
        adm = 0
        for q in JUNK_QUERIES:
            vec_ids = _vector_ids(q)
            hits = _hybrid_ids(q, vec_ids) if hybrid else vec_ids
            if hits:
                adm += 1
        return adm

    nj = len(JUNK_QUERIES)
    v_junk = _junk_admitted(hybrid=False)
    h_junk = _junk_admitted(hybrid=True)

    return {
        "sampled": len(hyb_ranks),
        "pool": len(pool),
        "k": k,
        "seed": seed,
        "sim_floor": _SIM_FLOOR,
        # AUTHORITATIVE — the shipped hybrid read path.
        "mrr": round(h_mrr, 4),
        f"recall_at_{k}": round(h_rec, 4),
        "top1": round(h_top1, 4),
        "junk_queries": nj,
        "junk_admitted": h_junk,
        "junk_rejection": round(1 - h_junk / nj, 4),
        # DIAGNOSTIC — the pure vector arm (for _SIM_FLOOR retuning).
        "vector_mrr": round(v_mrr, 4),
        f"vector_recall_at_{k}": round(v_rec, 4),
        "vector_top1": round(v_top1, 4),
        "vector_junk_rejection": round(1 - v_junk / nj, 4),
    }


# Floors match tests/sense/test_memory_eval_gate.py. Deliberately far below
# the measured numbers (2026-07-18, 400 samples x 3 seeds: MRR 0.700,
# recall@10 0.926, junk 1.000): these are CATASTROPHE floors, not targets. A
# floor set near the current value fires on ordinary variance and gets muted.
_MRR_FLOOR = 0.20
_RECALL_FLOOR = 0.35
_JUNK_FLOOR = 0.70

# Below this, a single run is noise. Variance across seeds is +-0.011 at
# n=400, and materially worse at n=60 — the target spec says plainly that a
# single 60-sample run must not be quoted as the system's state. The eval is
# cheap to run and expensive to misread, so the caveat travels WITH the
# numbers rather than living in a doc nobody opens.
_NOISY_BELOW = 100


def evaluate_recall(sample: int = 120, k: int = 10, seed: int = 7) -> dict:
    """Run the eval AND adjudicate it against the floors.

    ONE implementation, two callers: the daily daemon task and the
    `memory_recall` MCP tool. They used to be one caller and a hand-rolled
    duplicate, which is how a gate and its dashboard drift apart — the daemon
    already carried the floor logic inline, so an MCP tool that re-derived it
    would have been free to disagree with the thing that actually alarms.

    Returns the raw run_eval dict PLUS `ok`, `breaches` and `noisy`. `ok` is
    None (not False) when the eval could not run at all — an absent
    measurement and a failed one are different facts, and collapsing them is
    how "the gate is green" comes to mean "the gate never ran".
    """
    try:
        r = run_eval(sample=sample, k=k, seed=seed)
    except Exception as exc:  # noqa: BLE001 — report, never raise
        return {"ok": None, "error": str(exc), "sample_requested": sample}

    mrr = r.get("mrr", 0.0)
    recall = r.get(f"recall_at_{k}", 0.0)
    junk = r.get("junk_rejection", 0.0)
    vector_recall = r.get(f"vector_recall_at_{k}", 0.0)

    breaches = []
    if mrr < _MRR_FLOOR:
        breaches.append(f"MRR {mrr:.3f} < {_MRR_FLOOR}")
    if recall < _RECALL_FLOOR:
        breaches.append(f"recall@{k} {recall:.3f} < {_RECALL_FLOOR}")
    if junk < _JUNK_FLOOR:
        breaches.append(f"junk_rejection {junk:.3f} < {_JUNK_FLOOR}")
    # Self-calibrating. The shipped read path is hybrid; hybrid falling below
    # the pure-vector arm means the RRF fusion regressed, which absolute
    # floors on the hybrid numbers structurally cannot see.
    if recall < vector_recall:
        breaches.append(
            f"hybrid recall {recall:.3f} < vector-only {vector_recall:.3f} "
            "(RRF fusion regressed)"
        )

    r["ok"] = not breaches
    r["breaches"] = breaches
    r["floors"] = {"mrr": _MRR_FLOOR, "recall": _RECALL_FLOOR,
                   "junk_rejection": _JUNK_FLOOR}
    if r.get("sampled", 0) < _NOISY_BELOW:
        r["noisy"] = (
            f"sampled {r.get('sampled')} < {_NOISY_BELOW} — this run is NOISE, "
            "not the system's state. Do not quote it as a measurement; re-run "
            "with sample>=400 for a number worth recording."
        )
    return r


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Evaluate okuro memory recall.")
    p.add_argument("--sample", type=int, default=40)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    res = run_eval(sample=a.sample, k=a.k, seed=a.seed)
    if a.json:
        print(json.dumps(res, indent=2))
        return 0
    k = res["k"]
    print()
    print(f"  sampled          {res['sampled']} of {res['pool']} eligible")
    print(f"  sim floor        {res['sim_floor']}")
    print(f"  {'arm':8} {'MRR':>7} {'recall@'+str(k):>10} {'top-1':>7} {'junk_rej':>9}")
    print(f"  {'HYBRID':8} {res['mrr']:>7} {res[f'recall_at_{k}']:>10} "
          f"{res['top1']:>7} {res['junk_rejection']:>9}   <- authoritative (shipped read path)")
    print(f"  {'vector':8} {res['vector_mrr']:>7} {res[f'vector_recall_at_{k}']:>10} "
          f"{res['vector_top1']:>7} {res['vector_junk_rejection']:>9}   <- _SIM_FLOOR diagnostic")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
