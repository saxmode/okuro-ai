# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Measurement harness for the cross-repo graph — evidence gathering +
#   precision/recall scoring against a labelled gold set. Phase-0 baseline so
#   every later precision/coverage fix is proven by a number, not asserted.
# index:
#   def core_token
#   def collect_cross_repo_anchors
#   def gather_evidence
#   def dump_evidence
#   def score
# AGENT_HEADER_END -->
"""IR-grade evaluation for cortex.codegraph.crossrepo.

Two jobs:
  1. gather_evidence / dump_evidence — for each anchor that spans >=2 repos,
     pull the actual source lines it appears on in each repo, so a human/LLM
     judge can label it real (genuine shared contract) vs spurious (coincidental
     name collision). Output is a JSONL gold-candidate file.
  2. score — given a labelled gold file, compute precision (overall + per kind),
     precision@k over cross_repo_search for seed queries, and recall against a
     hand-curated known-true contract set.

The gold set is environment-specific (tied to the live managed repos), so this
is a live-eval harness, not a hermetic unit test. Re-run after every fix and
compare the delta.
"""

from __future__ import annotations

import json
from pathlib import Path


def core_token(anchor: str) -> str:
    """'sym:Frontmatter' -> 'Frontmatter'; 'route:/api/files' -> '/api/files'."""
    return anchor.split(":", 1)[1] if ":" in anchor else anchor


def _repo_roots(db) -> dict[str, Path]:
    rows = db.fetchall(
        "SELECT project_id AS pid, path AS root FROM managed_repos WHERE status='ready'"
    )
    return {r["pid"]: Path(r["root"]) for r in rows if r["root"]}


def collect_cross_repo_anchors(db) -> list[dict]:
    """Cross-repo anchors (kind-aware) with members — delegates to the graph's
    own index so eval and queries share one definition of 'cross-repo'."""
    from okuro.cortex.codegraph import cross_repo_index
    out = [
        {"anchor": a, "kind": e["kind"], "projects": e["projects"],
         "members": e["members"]}
        for a, e in cross_repo_index(db).items()
    ]
    out.sort(key=lambda a: (-len(a["projects"]), a["anchor"]))
    return out


def gather_evidence(db, item: dict, max_per_repo: int = 2) -> list[dict]:
    """Actual source lines the anchor's core token appears on, per repo."""
    roots = _repo_roots(db)
    token = core_token(item["anchor"])
    seen_repo: dict[str, int] = {}
    ev: list[dict] = []
    for m in item["members"]:
        if not m["file"]:  # repo-level owner node (no file)
            continue
        if seen_repo.get(m["project"], 0) >= max_per_repo:
            continue
        root = roots.get(m["project"])
        if not root:
            continue
        fpath = root / m["file"]
        try:
            lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if token in line:
                ev.append({"project": m["project"], "file": m["file"],
                           "line": i, "text": line.strip()[:160]})
                seen_repo[m["project"]] = seen_repo.get(m["project"], 0) + 1
                if seen_repo[m["project"]] >= max_per_repo:
                    break
    return ev


def dump_evidence(path: str) -> int:
    """Write one JSONL row per cross-repo anchor with evidence. Returns count."""
    from okuro.db import get_db
    db = get_db()
    items = collect_cross_repo_anchors(db)
    with open(path, "w", encoding="utf-8") as fh:
        for it in items:
            it = dict(it)
            it["evidence"] = gather_evidence(db, it)
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    return len(items)


def score(gold_path: str, seed_queries: list[str] | None = None,
          known_true: list[str] | None = None, k: int = 10,
          live_anchors: set[str] | None = None) -> dict:
    """Precision (overall + per kind), precision@k, recall vs known_true.

    ``live_anchors`` overrides the current cross-repo set (for tests); by
    default it is collected from the live DB.
    """
    from okuro.db import get_db
    from okuro.cortex.codegraph import cross_repo_search

    gold: dict[str, str] = {}
    with open(gold_path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            row = json.loads(ln)
            gold[row["anchor"]] = row["label"]  # 'real' | 'spurious'

    # Precision is measured over what the system CURRENTLY reports as cross-repo
    # (live ∩ gold) — an anchor a fix has since dropped is no longer a false
    # positive. New live anchors absent from the gold are surfaced as unlabelled.
    if live_anchors is None:
        live_anchors = {a["anchor"] for a in collect_cross_repo_anchors(get_db())}
    live = live_anchors
    unlabelled = sorted(a for a in live if a not in gold)

    per_kind: dict[str, list[int]] = {}
    real = total = 0
    for anchor in live:
        if anchor not in gold:
            continue
        kind = anchor.split(":", 1)[0]
        hit = 1 if gold[anchor] == "real" else 0
        per_kind.setdefault(kind, []).append(hit)
        real += hit
        total += 1
    precision = round(real / total, 3) if total else 0.0
    kind_prec = {kk: round(sum(v) / len(v), 3) for kk, v in per_kind.items()}

    # Recall vs the gold's real set: of anchors we KNOW are real, how many did
    # the system keep live? (drops here are precision-for-recall regressions.)
    gold_reals = [a for a, lb in gold.items() if lb == "real"]
    kept_reals = [a for a in gold_reals if a in live]
    dropped_reals = [a for a in gold_reals if a not in live]
    recall_gold = {
        "kept": len(kept_reals), "of": len(gold_reals),
        "recall": round(len(kept_reals) / len(gold_reals), 3) if gold_reals else 0.0,
        "dropped_reals": dropped_reals,
    }

    # Precision@k: for each seed query, fraction of top-k results labelled real.
    patk = {}
    for q in (seed_queries or []):
        res = cross_repo_search(q, limit=k)["results"]
        xr = [r for r in res if r.get("cross_repo")][:k]
        if xr:
            hits = sum(1 for r in xr if gold.get(r["anchor"]) == "real")
            patk[q] = round(hits / len(xr), 3)

    # Recall: of the hand-curated real contracts, how many surfaced cross-repo?
    recall = None
    if known_true:
        found = sum(1 for a in known_true if a in live)
        recall = {"found": found, "of": len(known_true),
                  "recall": round(found / len(known_true), 3),
                  "missing": [a for a in known_true if a not in live]}

    return {
        "live_cross_repo": len(live),
        "n_labelled_live": total,
        "precision_overall": precision,
        "precision_by_kind": kind_prec,
        "precision_at_k": patk,
        "recall_gold_reals": recall_gold,
        "recall_known_true": recall,
        "unlabelled_live": unlabelled,
    }
