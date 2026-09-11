# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro memory — inspect memory-utility signal (Meta-Harness P8).
# index: imports | def memory | def utility | def audit | def summary | def subjects
# AGENT_HEADER_END -->
"""okuro memory — inspect memory-utility signal."""

from __future__ import annotations

import click

from .output import console, ok, warn, info, heading, data_table


@click.group()
def memory():
    """Inspect memory utility (surfacings vs session scores)."""


@memory.command()
def summary():
    """High-level counters — how much signal do we have?"""
    from okuro.sense.memory_utility import summary as mu_summary

    s = mu_summary()
    heading("Memory-utility summary")
    info(f"Total surfacings:          {s.get('surfacings') or 0:,}")
    info(f"Unique memories surfaced:  {s.get('memories_surfaced') or 0:,}")
    info(f"Sessions with surfacings:  {s.get('sessions_with_surface') or 0:,}")
    info(f"Surfacings w/o session id: {s.get('surfacings_without_session') or 0:,}")
    b = s.get("baseline")
    info(f"Baseline compliance:       {b:.3f}" if b is not None else "Baseline compliance:       (no scored sessions)")


@memory.command()
@click.option("--min-surfacings", default=5, show_default=True)
@click.option("--limit", default=20, show_default=True)
@click.option("--reverse", is_flag=True, help="Show bottom-N by delta instead of top-N.")
def utility(min_surfacings: int, limit: int, reverse: bool):
    """Rank memories by delta (avg score of surfacing sessions − baseline)."""
    from okuro.sense.memory_utility import compute_utility

    result = compute_utility(min_surfacings=min_surfacings, limit=limit)
    b = result.get("baseline")
    heading("Memory utility")
    info(f"Baseline:       {b:.3f}" if b is not None else "Baseline:       (no scored sessions)")
    info(f"Memories shown: {result['total_memories']}  (min_surfacings={min_surfacings})")
    rows = list(result["stats"])
    if reverse:
        rows.reverse()
    if not rows:
        warn("No memories meet the min_surfacings threshold yet. More sessions will build signal over time.")
        return
    console.print(data_table(
        ["memory", "topic", "surf", "avg", "delta", "content"],
        [
            [
                (r["memory_id"] or "?")[:8],
                (r["topic"] or "-"),
                str(r["surfacings"]),
                f"{(r['avg_score'] or 0):.2f}",
                f"{r['delta']:+.2f}",
                (r.get("content_head") or "")[:60],
            ]
            for r in rows
        ],
    ))


@memory.command()
@click.option("--min-surfacings", default=20, show_default=True)
@click.option("--flag-threshold", default=-0.10, show_default=True)
def audit(min_surfacings: int, flag_threshold: float):
    """Flag memories whose surfacing correlates with lower compliance."""
    from okuro.sense.memory_utility import audit as mu_audit

    result = mu_audit(min_surfacings=min_surfacings, flag_threshold=flag_threshold)
    heading("Memory audit — negative-utility candidates")
    info(f"Baseline:       {result.get('baseline')}")
    info(f"Threshold:      delta ≤ {flag_threshold}")
    info(f"Min surfacings: {min_surfacings}")
    info(f"Flagged:        {result['total_memories']}")
    if not result["stats"]:
        ok("Nothing flagged. Either all memories are pulling their weight or there isn't enough signal yet.")
        return
    console.print(data_table(
        ["memory", "topic", "surf", "avg", "delta", "content"],
        [
            [
                (r["memory_id"] or "?")[:8],
                (r["topic"] or "-"),
                str(r["surfacings"]),
                f"{(r['avg_score'] or 0):.2f}",
                f"{r['delta']:+.2f}",
                (r.get("content_head") or "")[:60],
            ]
            for r in result["stats"]
        ],
    ))


@memory.command()
@click.option("--dry-run", is_flag=True, help="Show what would decay without writing.")
@click.option("--min-surfacings", default=20, show_default=True)
@click.option("--flag-threshold", default=-0.10, show_default=True)
@click.option("--step", default=0.05, show_default=True)
def decay(dry_run: bool, min_surfacings: int, flag_threshold: float, step: float):
    """Decay confidence on memories with sustained-negative utility."""
    from okuro.sense.memory_utility import auto_decay

    heading("Memory utility decay")
    r = auto_decay(
        min_surfacings=min_surfacings,
        flag_threshold=flag_threshold,
        step=step,
        dry_run=dry_run,
    )
    info(f"Candidates:    {r['candidates']}")
    info(f"Decayed:       {r['decayed']}")
    info(f"Cooldown skip: {r.get('skipped_cooldown', 0)}")
    info(f"Step:          {r['step']}")
    info(f"Dry-run:       {r['dry_run']}")
    for mid in r.get("memory_ids") or []:
        console.print(f"  - {mid}")
    if not r["candidates"]:
        ok("Nothing eligible. Either all memories are neutral/positive or signal hasn't accumulated yet.")


@memory.command()
@click.option("--sample-k", default=5, show_default=True, help="Top memories sampled per provisional.")
@click.option("--neighbors", default=25, show_default=True, help="kNN neighbors per sampled memory.")
@click.option("--merge-threshold", default=0.5, show_default=True, help="Min raw cosine share to merge.")
@click.option("--min-confidence", default=0.3, show_default=True, help="Min memory confidence sampled.")
@click.option("--lift-min", default=1.5, show_default=True, help="Min lift (share vs base rate) to merge.")
@click.option("--min-support", default=3, show_default=True, help="Min neighbor memories backing a merge.")
@click.option("--slug-min", default=0.85, show_default=True, help="Min slug similarity to treat as a naming duplicate.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON instead of a table.")
@click.option("--apply", "do_apply", is_flag=True, help="RATIFY: apply the proposed merges (rewrites memory tags).")
@click.option("--only", "only", multiple=True, help="With --apply, restrict to these provisional slugs.")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip the confirmation prompt.")
def subjects(sample_k, neighbors, merge_threshold, min_confidence, lift_min,
             min_support, slug_min, as_json, do_apply, only, assume_yes):
    """Propose (dry-run) or --apply merge/promote for provisional projects.

    Semantic (kNN over vec_memory) classification of each provisional against
    the canonical subjects. Without --apply this is ZERO mutation — advisory
    only. Merges are naming duplicates (slug identity + semantic dominance);
    related-but-distinct projects stay separate.
    """
    from okuro.sense.subjects import classify_provisionals, apply_subject_merge

    r = classify_provisionals(
        sample_k=sample_k,
        neighbors=neighbors,
        merge_threshold=merge_threshold,
        min_confidence=min_confidence,
        lift_min=lift_min,
        min_support=min_support,
        slug_min=slug_min,
    )

    if as_json and not do_apply:
        import json as _json
        # Plain echo — rich's console.print soft-wraps and can inject control
        # chars / markup, which corrupts machine-readable JSON.
        click.echo(_json.dumps(r, indent=2))
        return

    proposals = r["proposals"]
    merges = [p for p in proposals if p["suggested"] == "merge"]

    heading("Provisional subjects — %s" % ("APPLY" if do_apply else "DRY RUN (zero mutation)"))
    info(f"{len(proposals)} provisionals · {len(merges)} merge · {len(proposals) - len(merges)} promote")

    if merges:
        console.print(data_table(
            ["provisional", "-> merge into", "share", "lift", "support", "sources"],
            [[p["id"], p["target"], f"{p['score']:.0%}", f"{p['lift']:.2f}",
              str(p["support"]), str(p["sources"])] for p in merges],
        ))
    promotes = [p for p in proposals if p["suggested"] == "promote"]
    if promotes and not do_apply:
        console.print(data_table(
            ["keep as distinct", "why"],
            [[p["id"], p["rationale"]] for p in promotes],
        ))

    if not do_apply:
        ok("Advisory only — nothing changed. Re-run with --apply to ratify merges.")
        return

    # --apply: mutate only the naming-duplicate merges (never promotes).
    targets = [p for p in merges if not only or p["id"] in only]
    if not targets:
        warn("No merges to apply." + (" (none matched --only)" if only else ""))
        return
    if not assume_yes:
        names = ", ".join(f"{p['id']}→{p['target']}" for p in targets)
        if not click.confirm(f"Apply {len(targets)} merge(s) [{names}]? This rewrites memory tags."):
            warn("Aborted — nothing changed.")
            return

    for p in targets:
        res = apply_subject_merge(p["id"], p["target"], dry_run=False)
        dropped = sum(res.get("dropped_as_duplicate", {}).values())
        ok(f"{p['id']} → {p['target']}: {res['total_rows'] - dropped} moved"
           + (f", {dropped} dup dropped" if dropped else "") + ", source retired")
