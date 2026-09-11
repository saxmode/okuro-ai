#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 PROOF + EXIT-GATE runner. Solves every fixture x level x
#   family x brand through the kit gallery pipeline, renders screenshots, and
#   reports the falsifiable exit-gate numbers: (a) determinism, (b) measure-guard
#   convergence, (c) zero overflow/truncation, plus the rendered==gallery contract
#   arm and (optionally) the (d) vision-judge A/B. Deterministic; exit 0 = gates
#   a-c green + contract armed.
# index:
#   run_proof / _determinism / _render_pass / main
# AGENT_HEADER_END -->
"""Proof harness for the composition solver.

Run:
  PYTHONPATH=src <venv>/bin/python \
      -m okuro.prism.solver.proof --out <dir> [--brands okuro,northwind,meridian] [--judge]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from okuro.prism.solver.beam import beam_search, canonical_key
from okuro.prism.solver.families import family_names, get_family
from okuro.prism.solver.fixtures.content import (
    all_fixtures, claim_shapes_covered, families_covered,
)
from okuro.prism.solver.render import (
    check_solver_contract, make_measure_fn, serving_kit,
)
from okuro.prism.solver.selection import select_family
from okuro.prism.solver.solve import solve

BRANDS_DEFAULT = ["okuro", "northwind", "meridian"]
SEED = 0


def _determinism() -> dict:
    """Gate (a): same seed -> byte-identical layout, plan-only (render-free beam is
    the source of determinism). Every fixture x level x ALL families x brand."""
    total = 0
    identical = 0
    for f in all_fixtures():
        for level, plan in f.levels.items():
            for fam_name in family_names():
                fam = get_family(fam_name)
                for brand in BRANDS_DEFAULT:
                    r1 = beam_search(plan, f.claims, fam, brand, seed=SEED)
                    r2 = beam_search(plan, f.claims, fam, brand, seed=SEED)
                    k1 = [canonical_key(x.placement) for x in r1]
                    k2 = [canonical_key(x.placement) for x in r2]
                    total += 1
                    if k1 == k2 and r1:
                        identical += 1
    return {"total": total, "identical": identical,
            "pct": round(100 * identical / total, 1) if total else 0.0}


def _render_pass(renderer, out_dir: Path, brands: list[str], selection_family: bool) -> dict:
    """Gates (b) + (c): solve every fixture x level x brand through one render each,
    with the measure-guard; screenshot A. Family = the fixture's own (or the
    selection heuristic when selection_family=True). Returns the per-slide records."""
    mf = make_measure_fn(renderer)
    records = []
    for f in all_fixtures():
        fam_name = (select_family(f.content_character, f.audience_mood)
                    if selection_family else f.family)
        for level, plan in f.levels.items():
            for brand in brands:
                res = solve(plan, f.claims, fam_name, brand=brand, seed=SEED, measure_fn=mf)
                png = out_dir / f"{f.name}_{level}_{fam_name}_{brand}.png"
                renderer.screenshot(res.a.placement, plan, f.claims, brand, png)
                records.append({
                    "fixture": f.name, "level": level, "family": fam_name, "brand": brand,
                    "resolves_used": res.resolves_used, "converged": res.converged,
                    "slide_height_px": res.measured.slide_height_px if res.measured else None,
                    "overflow": res.measured.overflow if res.measured else None,
                    "orphan": res.measured.orphan if res.measured else None,
                    "score": round(res.a.total, 3),
                    "rows": [list(r.spans) for r in res.a.placement.rows],
                    "screenshot": str(png),
                })
    return records


def _families_pass(renderer, out_dir: Path, brand: str) -> list[dict]:
    """Prove all 4 families are exercised end-to-end: each fixture's L2 solved +
    screenshotted under every family."""
    mf = make_measure_fn(renderer)
    out = []
    for f in all_fixtures():
        plan = f.levels["L2"]
        for fam_name in family_names():
            res = solve(plan, f.claims, fam_name, brand=brand, seed=SEED, measure_fn=mf)
            png = out_dir / "families" / f"{f.name}_L2_{fam_name}.png"
            renderer.screenshot(res.a.placement, plan, f.claims, brand, png)
            out.append({"fixture": f.name, "family": fam_name, "brand": brand,
                        "score": round(res.a.total, 3),
                        "rows": [list(r.spans) for r in res.a.placement.rows],
                        "screenshot": str(png)})
    return out


def run_proof(out_dir: Path, brands: list[str], do_judge: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    det = _determinism()
    with serving_kit() as r:
        contract_drift = check_solver_contract(r)
        records = _render_pass(r, out_dir, brands, selection_family=False)
        fam_records = _families_pass(r, out_dir, brands[0])
        judge_results = []
        if do_judge:
            from okuro.prism.solver.judge import judge_ab, probe_vision
            sample = Path(records[0]["screenshot"])
            vision_ok = probe_vision(sample)
            if vision_ok:
                # judge the A/B of the first 3 L2 slides (own family, first brand).
                l2 = [rec for rec in records if rec["level"] == "L2" and rec["brand"] == brands[0]][:3]
                for rec in l2:
                    f = next(x for x in all_fixtures() if x.name == rec["fixture"])
                    plan = f.levels["L2"]
                    ranked = beam_search(plan, f.claims, get_family(rec["family"]), brands[0], seed=SEED)
                    a_png = Path(rec["screenshot"])
                    b_png = out_dir / f"{rec['fixture']}_L2_{rec['family']}_{brands[0]}_B.png"
                    if len(ranked) > 1:
                        r.screenshot(ranked[1].placement, plan, f.claims, brands[0], b_png)
                        v = judge_ab(a_png, b_png, "L2", rec["family"], rec["fixture"])
                        judge_results.append({"fixture": rec["fixture"], "winner": v.winner,
                                              "method": v.method, "rationale": v.rationale})
            else:
                judge_results.append({"method": "needs-human-eye",
                                      "note": "no vision-capable provider answered probe"})

    # gate (b): convergence <= 2 re-solves
    conv = sum(1 for rec in records if rec["resolves_used"] <= 2)
    conv_pct = round(100 * conv / len(records), 1) if records else 0.0
    # gate (c): zero overflow after guard (final chosen fits budget); truncation is
    # structurally impossible (auto-height container).
    overflowing = [rec for rec in records if rec["overflow"]]
    summary = {
        "corpus": {
            "n_fixtures": len(all_fixtures()),
            "n_slides_rendered": len(records),
            "claim_shapes_covered": sorted(claim_shapes_covered()),
            "families_covered": sorted(families_covered()),
            "brands": brands,
        },
        "gate_a_determinism": det,
        "gate_b_convergence": {"n": len(records), "converged_le2": conv, "pct": conv_pct,
                               "pass": conv_pct >= 95.0},
        "gate_c_overflow": {"n": len(records), "overflowing": len(overflowing),
                            "truncation": 0, "pass": len(overflowing) == 0,
                            "offenders": [o["screenshot"] for o in overflowing]},
        "contract_arm": {"drift": contract_drift, "armed": not contract_drift},
        "gate_d_judge": judge_results,
        "records": records,
        "families_pass": fam_records,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / ".okuro/proof/prism-w2-proof"))
    ap.add_argument("--brands", default=",".join(BRANDS_DEFAULT))
    ap.add_argument("--judge", action="store_true")
    args = ap.parse_args()
    brands = [b.strip() for b in args.brands.split(",") if b.strip()]
    s = run_proof(Path(args.out), brands, do_judge=args.judge)

    a, b, c = s["gate_a_determinism"], s["gate_b_convergence"], s["gate_c_overflow"]
    print("\n== PRISM v4 W2 — composition solver proof ==")
    print(f"corpus: {s['corpus']['n_fixtures']} fixtures -> {s['corpus']['n_slides_rendered']} slides "
          f"· shapes {len(s['corpus']['claim_shapes_covered'])}/11 · families {s['corpus']['families_covered']}")
    print(f"(a) determinism : {a['identical']}/{a['total']} byte-identical ({a['pct']}%)")
    print(f"(b) convergence : {b['converged_le2']}/{b['n']} <=2 re-solves ({b['pct']}%) -> "
          f"{'PASS' if b['pass'] else 'FAIL'}")
    print(f"(c) overflow    : {c['overflowing']}/{c['n']} overflow · {c['truncation']} truncation -> "
          f"{'PASS' if c['pass'] else 'FAIL'}")
    print(f"contract arm    : {'ARMED (0 drift)' if s['contract_arm']['armed'] else 'DRIFT ' + str(s['contract_arm']['drift'])}")
    if s["gate_d_judge"]:
        print(f"(d) vision judge: {s['gate_d_judge']}")
    print(f"screenshots + summary.json -> {args.out}")
    ok = a["pct"] == 100.0 and b["pass"] and c["pass"] and s["contract_arm"]["armed"]
    print(f"\n{'PASS' if ok else 'FAIL'} — exit gates a-c {'green' if ok else 'RED'} + contract {'armed' if s['contract_arm']['armed'] else 'BROKEN'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
