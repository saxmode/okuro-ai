#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W3 E2E PROOF + EXIT-GATE runner. Runs the FULL new authoring
#   pipeline on the E2E fixture topic: author_ladder -> ladder gate (0 violations
#   incl L4-coverage) -> accuracy accounting (clean) -> solve + render each level
#   L1/L2/L3 + an L4 reading render, across 2 brands (okuro + northwind). Optional
#   level-matched vision judge vs an optional hand-authored reference. Deterministic;
#   exit 0 = ladder gate clean + accuracy clean + zero overflow across renders.
# index:
#   compose_l4_html / run_e2e / main
# AGENT_HEADER_END -->
"""End-to-end proof for W3 authoring — one fixture topic, L1-L4, two brands.

Run:
  PYTHONPATH=src <venv>/bin/python \
      -m okuro.prism.authoring.proof --out <dir> [--brands okuro,northwind] [--judge]
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

from okuro.prism.authoring.accuracy import account
from okuro.prism.authoring.fixtures import e2e_topics, reference_html
from okuro.prism.authoring.gate import check_ladder
from okuro.prism.authoring.ladder import author_ladder
from okuro.prism.solver.render import _CSS, make_measure_fn, serving_kit
from okuro.prism.solver.solve import solve

BRANDS_DEFAULT = ["okuro", "northwind"]
LEVELS = ("L1", "L2", "L3")
SEED = 0


def compose_l4_html(l4, master_doc: str, brand: str, css_base: str) -> str:
    """L4 reading layout — the topic's full verbatim text, beautifully set in a
    reading column over the kit CSS (the resolution ladder's full-resolution end)."""
    links = "\n".join(f'<link rel="stylesheet" href="{css_base}/{c}">' for c in _CSS)
    paras = "".join(f"<p>{html.escape(p.strip())}</p>"
                    for p in master_doc.split("\n\n") if p.strip())
    return (
        f'<!doctype html><html lang="en" data-theme="{brand}"><head><meta charset="utf-8">'
        f'{links}</head><body>'
        f'<div class="composed-slide" data-level="L4">'
        f'<div class="composed-row" style="grid-template-columns:repeat(12,1fr)">'
        f'<div class="composed-cell" data-component="statement-title" data-emphasis="focal" '
        f'style="grid-column:span 12">'
        f'<div class="slide-title"><span class="accent">{html.escape(l4.title)}</span></div></div>'
        f'</div>'
        f'<div class="composed-row" style="grid-template-columns:repeat(12,1fr)">'
        f'<div class="composed-cell" style="grid-column:span 8">'
        f'<div class="prose">{paras}</div></div></div>'
        f'</div></body></html>'
    )


def run_e2e(out_dir: Path, brands: list[str], do_judge: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    topics = e2e_topics()
    topic_reports: list[dict] = []

    with serving_kit() as r:
        mf = make_measure_fn(r)
        for topic in topics:
            ladder = author_ladder(topic)
            gate = check_ladder(ladder, topic)
            acc = account(ladder, gate)

            renders: list[dict] = []
            for brand in brands:
                for level in LEVELS:
                    plan = ladder.plans[level]
                    res = solve(plan, ladder.claims, ladder.family, brand=brand,
                                seed=SEED, measure_fn=mf)
                    png = out_dir / f"{topic.topic_id}_{level}_{ladder.family}_{brand}.png"
                    r.screenshot(res.a.placement, plan, ladder.claims, brand, png)
                    renders.append({
                        "level": level, "brand": brand, "family": ladder.family,
                        "height_px": res.measured.slide_height_px if res.measured else None,
                        "overflow": res.measured.overflow if res.measured else None,
                        "converged": res.converged, "screenshot": str(png),
                    })
                # L4 reading render
                l4png = out_dir / f"{topic.topic_id}_L4_{brand}.png"
                r.page.set_viewport_size({"width": 1920, "height": 1080})
                r.page.set_content(compose_l4_html(ladder.l4, topic.master_doc, brand, r.base),
                                   wait_until="networkidle")
                r.page.wait_for_timeout(60)
                el = r.page.query_selector(".composed-slide")
                el.screenshot(path=str(l4png))
                renders.append({"level": "L4", "brand": brand, "family": ladder.family,
                                "screenshot": str(l4png)})

            judge_results: list[dict] = []
            if do_judge:
                judge_results = _run_judge(r, out_dir, topic, ladder, brands[0])

            overflowing = [x for x in renders if x.get("overflow")]
            topic_reports.append({
                "topic": topic.topic_id,
                "family": ladder.family,
                "loads": ladder.loads,
                "ink_density": {k: round(v, 4) for k, v in ladder.densities.items()},
                "gate": {
                    "passed": gate.passed,
                    "violations": [f"{v.check}/{v.level}: {v.detail}" for v in gate.violations],
                    "l4_coverage": f"{len(gate.l4_covered)}/{len(gate.l4_covered)+len(gate.l4_missing)}",
                    "l4_missing": gate.l4_missing,
                },
                "accuracy": {
                    "clean": acc.clean,
                    "silent_deficits": acc.silent_deficits,
                    "count_mismatches": acc.count_mismatches,
                    "synthesized": acc.synthesized,
                    "deckdoc": acc.to_deckdoc(),
                },
                "renders": renders,
                "overflow": len(overflowing),
                "judge": judge_results,
            })

    gate_clean = all(t["gate"]["passed"] for t in topic_reports)
    acc_clean = all(t["accuracy"]["clean"] for t in topic_reports)
    zero_overflow = all(t["overflow"] == 0 for t in topic_reports)
    summary = {
        "topics": topic_reports,
        "brands": brands,
        "exit": {
            "ladder_gate_clean": gate_clean,
            "accuracy_clean": acc_clean,
            "zero_overflow": zero_overflow,
            "pass": gate_clean and acc_clean and zero_overflow,
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def _run_judge(r, out_dir: Path, topic, ladder, brand: str) -> list[dict]:
    """Level-matched judge (R31): L1 sparse-hook (absolute), L2/L3 vs the optional
    reference (L2 match, L3 ref-as-floor). needs-human-eye when no vision provider."""
    from okuro.prism.solver.judge import judge_level, probe_vision
    results: list[dict] = []
    sample = out_dir / f"{topic.topic_id}_L1_{ladder.family}_{brand}.png"
    if not probe_vision(sample):
        return [{"method": "needs-human-eye", "note": "no vision-capable provider answered probe"}]
    ref = reference_html()
    ref_png = out_dir / f"{topic.topic_id}_reference.png"
    try:
        r.page.set_viewport_size({"width": 1600, "height": 1200})
        r.page.goto(ref.as_uri(), wait_until="networkidle")
        r.page.wait_for_timeout(120)
        r.page.screenshot(path=str(ref_png), full_page=False)
    except Exception as exc:
        ref_png = None
        results.append({"note": f"reference render failed: {exc}"})
    shallower = {"L2": "L1", "L3": "L2"}
    for level in LEVELS:
        a_png = out_dir / f"{topic.topic_id}_{level}_{ladder.family}_{brand}.png"
        sp = (out_dir / f"{topic.topic_id}_{shallower[level]}_{ladder.family}_{brand}.png"
              if level in shallower else None)
        v = judge_level(a_png, level, ladder.family,
                        reference_png=(ref_png if level != "L1" else None),
                        shallower_png=sp)
        results.append({"level": level, "winner": v.winner, "method": v.method,
                        "scores_a": v.scores_a, "scores_b": v.scores_b,
                        "rationale": v.rationale})
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / ".okuro/proof/prism-w3-proof"))
    ap.add_argument("--brands", default=",".join(BRANDS_DEFAULT))
    ap.add_argument("--judge", action="store_true")
    args = ap.parse_args()
    brands = [b.strip() for b in args.brands.split(",") if b.strip()]
    s = run_e2e(Path(args.out), brands, do_judge=args.judge)

    print("\n== PRISM v4 W3 — E2E authoring proof ==")
    for t in s["topics"]:
        g, a = t["gate"], t["accuracy"]
        print(f"topic {t['topic']} · family {t['family']} · loads {t['loads']}")
        print(f"  ladder gate : {'CLEAN' if g['passed'] else 'FAIL ' + str(g['violations'])} "
              f"· L4-coverage {g['l4_coverage']}")
        print(f"  accuracy    : {'CLEAN' if a['clean'] else 'DIRTY'} "
              f"· silent-deficits {len(a['silent_deficits'])} · count-mismatch {len(a['count_mismatches'])}")
        print(f"  renders     : {len(t['renders'])} · overflow {t['overflow']}")
        if t["judge"]:
            print(f"  judge       : {t['judge']}")
    ok = s["exit"]["pass"]
    print(f"\n{'PASS' if ok else 'FAIL'} — ladder {s['exit']['ladder_gate_clean']} · "
          f"accuracy {s['exit']['accuracy_clean']} · zero-overflow {s['exit']['zero_overflow']}")
    print(f"screenshots + summary.json -> {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
