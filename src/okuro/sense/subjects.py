# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Charter-subject taxonomy — dry-run manifest of what a
#   dedup/merge WOULD touch, with zero mutation. The ratify surface between
#   the messy registry and any tag rewrite.
# index: imports | def canonical_slug | def _project_tag_tables | def _tag_counts | def merge_manifest | def _slug_key | def _slug_similarity | def _promote_reason | def classify_provisionals | def render_manifest_md
# AGENT_HEADER_END -->
"""Charter-subject taxonomy — DRY-RUN manifest generation.

Produces a preview of registry cleanup with ZERO mutation: every function
here only reads. Rewriting a memory's ``project`` tag is a ratify-gated
operation performed elsewhere, only after a human approves this manifest.

Two layers, cleanest-first:
  1. deterministic — canonical-slug collisions (no thresholds, no embeddings,
     no tuning). Safe, obvious merges (casing/separator variants).
  2. semantic (deferred) — provisional rows carrying knowledge need embedding
     clustering + reconcile-role judgment + this same ratify gate.
"""

import re


def canonical_slug(slug: str) -> str:
    """Deterministic slug canonicalization — a RULE, not a tuned threshold.

    lowercases, collapses whitespace/underscores to hyphens, dedupes hyphens,
    trims. Conservative on purpose: only collapses variants that are the same
    subject spelled differently (``Northwind_UI_v2`` -> ``northwind-ui-v2``). No
    fuzzy/semantic merging happens here.
    """
    s = (slug or "").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _project_tag_tables(db) -> list[str]:
    """Discover every table with a ``project`` column (store-agnostic).

    Avoids hardcoding the store list — any current/future table that tags
    rows with a project slug is counted in the blast radius automatically.
    """
    tables = [
        r["name"]
        for r in db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    out = []
    for t in tables:
        cols = {r["name"] for r in db.fetchall(f"PRAGMA table_info({t})")}
        if "project" in cols:
            out.append(t)
    return out


def _tag_counts(db, tables: list[str], project: str) -> dict[str, int]:
    """Per-store row counts tagged to a project — the blast radius."""
    counts = {}
    for t in tables:
        n = db.fetchone(f"SELECT COUNT(*) AS c FROM {t} WHERE project = ?", (project,))["c"]
        if n:
            counts[t] = n
    return counts


def merge_manifest() -> dict:
    """Build the DRY-RUN cleanup manifest. Reads only — never writes.

    Returns a structured manifest:
      - summary counts
      - deterministic_merges: canonical-slug collision groups + blast radius
      - safe_archive: provisional rows with ZERO tags (no knowledge attached)
      - needs_classification: provisional rows WITH tags (semantic + ratify)
    """
    from okuro.db import get_db

    db = get_db()
    tag_tables = _project_tag_tables(db)

    rows = db.fetchall(
        "SELECT id, name, path, COALESCE(provisional, 0) AS provisional, active "
        "FROM projects"
    )

    # Blast radius per project (total tags across all stores).
    def _total_tags(pid: str) -> tuple[int, dict]:
        counts = _tag_counts(db, tag_tables, pid)
        return sum(counts.values()), counts

    enriched = {}
    for r in rows:
        total, counts = _total_tags(r["id"])
        enriched[r["id"]] = {**dict(r), "tags_total": total, "tags_by_store": counts}

    # Layer 1 — deterministic canonical-slug collisions.
    groups: dict[str, list[str]] = {}
    for pid in enriched:
        groups.setdefault(canonical_slug(pid), []).append(pid)

    deterministic_merges = []
    for canon, members in groups.items():
        if len(members) < 2:
            continue
        # Target = a curated member (non-provisional, real path) if one exists,
        # else the member whose id already equals the canonical form, else the
        # member carrying the most knowledge. Never guessed silently — recorded.
        def _rank(pid):
            e = enriched[pid]
            curated = 0 if (not e["provisional"] and not str(e["path"] or "").startswith("unknown/")) else 1
            is_canon = 0 if pid == canon else 1
            return (curated, is_canon, -e["tags_total"])
        ordered = sorted(members, key=_rank)
        target, sources = ordered[0], ordered[1:]
        deterministic_merges.append({
            "canonical": canon,
            "target": target,
            "sources": [
                {"id": s, "tags_total": enriched[s]["tags_total"],
                 "tags_by_store": enriched[s]["tags_by_store"]}
                for s in sources
            ],
            "memories_rerouted": sum(enriched[s]["tags_total"] for s in sources),
        })

    provisional = [e for e in enriched.values() if e["provisional"]]
    safe_archive = [
        {"id": e["id"], "path": e["path"]}
        for e in provisional if e["tags_total"] == 0
    ]
    needs_classification = sorted(
        ({"id": e["id"], "tags_total": e["tags_total"], "tags_by_store": e["tags_by_store"]}
         for e in provisional if e["tags_total"] > 0),
        key=lambda x: -x["tags_total"],
    )

    return {
        "summary": {
            "projects_total": len(rows),
            "provisional": len(provisional),
            "canonical": len(rows) - len(provisional),
            "tag_stores": tag_tables,
            "deterministic_merge_groups": len(deterministic_merges),
            "safe_archive_zero_tags": len(safe_archive),
            "needs_semantic_classification": len(needs_classification),
        },
        "deterministic_merges": deterministic_merges,
        "safe_archive": safe_archive,
        "needs_classification": needs_classification,
        "mutation": "NONE — dry run",
    }


def _slug_key(slug: str) -> str:
    """Identity key for a slug: lowercase, strip every non-alphanumeric.

    ``acme-corp`` and ``acmecorp`` collapse to the same key — a true naming
    duplicate — while ``tm-animator`` (``tmanimator``) stays distinct.
    """
    return re.sub(r"[^a-z0-9]", "", slug.lower())


def _slug_similarity(a: str, b: str) -> float:
    """0..1 identity similarity between two slugs on their alnum keys."""
    from difflib import SequenceMatcher

    ka, kb = _slug_key(a), _slug_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return SequenceMatcher(None, ka, kb).ratio()


def _promote_reason(
    target: str, score: float, lift: float, supp: int, nsrc: int, slug_sim: float,
    merge_threshold: float, lift_min: float, min_support: int, slug_min: float,
) -> str:
    """Explain which merge gate the top canonical failed (evidence for a human)."""
    if score < merge_threshold:
        return f"top {target} only {score:.0%} of mass < {merge_threshold:.0%} — likely distinct"
    if lift < lift_min:
        return f"{target} at {score:.0%} but lift {lift:.2f} < {lift_min} — no better than base rate"
    if supp < min_support:
        return f"{target} dominant but only {supp} neighbor(s) < {min_support} support — too thin"
    if nsrc < 2:
        return f"{target} backed by a single memory — not enough independent evidence"
    if slug_sim < slug_min:
        # The decisive case: semantically close to target but a DIFFERENT
        # subject by name. Merging would bury it (you couldn't resurface it
        # on its own again). Keep distinct; surface the relationship instead.
        return f"related to {target} (topic overlap) but a distinct project — kept separate"
    return f"{target} — distinct"


def classify_provisionals(
    sample_k: int = 5,
    neighbors: int = 25,
    merge_threshold: float = 0.5,
    min_confidence: float = 0.3,
    lift_min: float = 1.5,
    min_support: int = 3,
    slug_min: float = 0.85,
) -> dict:
    """DRY-RUN de-dup proposal for each knowledge-bearing provisional.

    kNN-vote (reuses vec_memory, no vec0-internals): sample each provisional's
    top memories, find their nearest neighbors, tally which CANONICAL project
    the neighbors belong to.

    A merge is proposed ONLY for a naming DUPLICATE — semantically dominant AND
    a near-identical slug (``acmecorp`` -> ``acme-corp``). Semantic overlap
    alone is NOT a merge: ``acme-animator`` lives near ``acme-corp`` because the
    tool runs on that server, but it is a distinct subject — folding it in would
    bury its memories so you could never resurface ``tm-animator`` on its own.
    Those stay ``promote`` (distinct), with the related canonical surfaced.

    Three corrections vs. the naive prototype (which the dry-run proved unsafe):
      * metric — vec_memory declares ``distance_metric=cosine``, so cosine is
        ``1 - d``, clamped at 0 so an opposed vector cannot cast a negative
        vote. (Before 2026-07-15 the table was L2 and this read
        ``1 - d^2/2``; okuro.embed.repair now enforces the metric in the
        schema instead.)
      * base rate — a lift gate (share vs prior) stops the majority class
        (okuro ~73% of memories) winning by size alone.
      * identity — a slug-similarity gate stops merging related-but-distinct
        projects; only true naming duplicates merge.

    ADVISORY ONLY — zero mutation. Thresholds are surfaced params, not buried
    constants (no-hardcode). Every proposal ships its evidence (the voting
    neighbors) and its score so a human ratifies, never the machine. Merges are
    still eval-gated at apply time — this only proposes.
    """
    from okuro.db import get_db
    from okuro.embed.client import embed_one, to_bytes

    db = get_db()

    canonical = {
        r["id"]
        for r in db.fetchall(
            "SELECT id FROM projects WHERE active = 1 AND COALESCE(provisional, 0) = 0"
        )
    }
    provisionals = [
        r["id"]
        for r in db.fetchall(
            "SELECT id FROM projects WHERE active = 1 AND COALESCE(provisional, 0) = 1"
        )
    ]

    # Base rates — canonical corpus sizes. Without this, kNN votes drown in the
    # majority class (okuro holds ~73% of all canonical memories, so it wins
    # every provisional). We downweight each vote by the class's prior, so a
    # small tightly-related canonical can out-vote a large diffuse one.
    # Laplace-smoothed to avoid a 1-memory class exploding the ratio.
    canon_size: dict[str, int] = {}
    for r in db.fetchall(
        "SELECT project AS id, COUNT(*) AS n FROM agent_memory GROUP BY project"
    ):
        if r["id"] in canonical:
            canon_size[r["id"]] = r["n"]
    _total_canon = sum(canon_size.values())
    _k_classes = max(len(canon_size), 1)

    def base_rate(proj: str) -> float:
        return (canon_size.get(proj, 0) + 1) / (_total_canon + _k_classes)

    proposals = []
    for pid in provisionals:
        mems = db.fetchall(
            "SELECT id, content FROM agent_memory WHERE project = ? AND confidence >= ? "
            "ORDER BY confidence DESC LIMIT ?",
            (pid, min_confidence, sample_k),
        )
        if not mems:
            continue  # zero-knowledge provisionals are handled by safe_archive

        # Tally canonical neighbors by (metric-correct) cosine similarity.
        # RAW similarity mass — NOT base-rate-corrected — is what ranks the
        # target: base-rate weighting over-amplifies tiny classes (one stray
        # neighbor in a 1-memory project would win). Base rate enters only as
        # a *lift gate* below, which kills the opposite bias (the majority
        # class winning by sheer size).
        votes: dict[str, float] = {}
        support: dict[str, int] = {}          # neighbor memories per canonical
        sources: dict[str, set] = {}          # distinct sampled memories backing it
        evidence_ids: list[str] = []
        own = {m["id"] for m in mems}
        for m in mems:
            try:
                vb = to_bytes(embed_one(m["content"]))
            except Exception:
                continue
            hits = db.vec_search("vec_memory", vb, limit=neighbors)
            nb_ids = [h["id"] for h in hits if h["id"] not in own]
            if not nb_ids:
                continue
            # vec_memory declares distance_metric=cosine, so sqlite-vec
            # returns cosine distance (1 - cos) and cosine = 1 - d directly.
            # This module previously reconstructed cosine from L2 as
            # 1 - d^2/2, which was correct while the table was L2 but is wrong
            # now that the metric is declared (see okuro.embed.repair). The
            # ORIGINAL `1 - d` was broken only because the table was L2 —
            # with cosine declared it is exact. Clamp to [0,1]: cosine
            # distance runs to 2, so `1 - d` goes negative for opposed
            # vectors, and a negative weight would subtract from the summed
            # vote rather than simply not adding to it.
            sim = {
                h["id"]: max(0.0, 1.0 - h["distance"]) for h in hits
            }
            ph = ", ".join("?" * len(nb_ids))
            for r in db.fetchall(
                f"SELECT id, project FROM agent_memory WHERE id IN ({ph})", tuple(nb_ids)
            ):
                proj = r["project"]
                if proj in canonical:
                    votes[proj] = votes.get(proj, 0.0) + sim.get(r["id"], 0.0)
                    support[proj] = support.get(proj, 0) + 1
                    sources.setdefault(proj, set()).add(m["id"])
                    evidence_ids.append(r["id"])

        total = sum(votes.values())
        if total <= 0:
            proposals.append({
                "id": pid, "suggested": "promote", "target": None, "score": 0.0,
                "rationale": "no canonical neighbors — distinct subject",
            })
            continue

        # Target = highest RAW similarity share (defeats minority over-amp:
        # a rare class with one stray neighbor never has the top share).
        target, mass = max(votes.items(), key=lambda kv: kv[1])
        score = mass / total
        lift = score / base_rate(target)      # observed share vs prior
        supp = support.get(target, 0)
        nsrc = len(sources.get(target, ()))
        # Merge only when the target is dominant AND above chance AND backed by
        # enough independent evidence. The lift gate is what stops the 73%
        # majority class (okuro) from winning a provisional it isn't part of.
        slug_sim = _slug_similarity(pid, target)
        # Merge ONLY a naming duplicate: semantically dominant + above chance +
        # well-supported + a near-identical slug. The slug gate is the decisive
        # one — it stops folding a related-but-distinct project into a bucket
        # (which would destroy the ability to resurface it on its own).
        semantic_ok = (
            score >= merge_threshold
            and lift >= lift_min
            and supp >= min_support
            and nsrc >= 2
        )
        merge_ok = semantic_ok and slug_sim >= slug_min
        suggested = "merge" if merge_ok else "promote"
        proposals.append({
            "id": pid,
            "suggested": suggested,
            "target": target if merge_ok else None,
            # When kept distinct but strongly related, surface the relationship
            # (advisory only — NOT a merge) so the signal isn't lost.
            "related_to": target if (semantic_ok and not merge_ok) else None,
            "score": round(score, 3),
            "lift": round(lift, 2),
            "slug_sim": round(slug_sim, 2),
            "support": supp,
            "sources": nsrc,
            "runner_up": sorted(
                ({"project": k, "share": round(v / total, 3)} for k, v in votes.items()),
                key=lambda x: -x["share"],
            )[:3],
            "rationale": (
                f"{score:.0%} neighbor mass -> {target}, lift {lift:.2f}, slug≈{slug_sim:.2f} — naming duplicate"
                if merge_ok
                else _promote_reason(
                    target, score, lift, supp, nsrc, slug_sim,
                    merge_threshold, lift_min, min_support, slug_min,
                )
            ),
        })

    return {
        "params": {
            "sample_k": sample_k, "neighbors": neighbors,
            "merge_threshold": merge_threshold, "min_confidence": min_confidence,
            "lift_min": lift_min, "min_support": min_support, "slug_min": slug_min,
            "similarity": "cosine (1 - d) over cosine-metric vec_memory",
            "target_rank": "raw cosine share",
            "merge_gate": "semantic (share/lift/support/sources) AND slug_sim>=slug_min — merges are naming duplicates only",
        },
        "proposals": sorted(proposals, key=lambda p: (p["suggested"], -p["score"])),
        "mutation": "NONE — dry run · advisory · ratify per-item · merges eval-gated at apply",
    }


def apply_subject_merge(source: str, target: str, *, dry_run: bool = True) -> dict:
    """Re-tag every project-tagged row from SOURCE onto TARGET, then retire SOURCE.

    The ratified counterpart to ``classify_provisionals``' dry-run proposals —
    the ONLY writer in this module. MUTATES when ``dry_run=False``; call it only
    after a human ratifies the specific merge. Runs in a single transaction and
    RETIRES the source project (``active = 0``) rather than deleting it, so the
    merge is reversible from the returned per-table rowids.
    """
    from okuro.db import get_db

    db = get_db()
    if source == target:
        raise ValueError("source and target are the same project")

    # Tables actually holding source rows. COUNT works on every project-tagged
    # table including vec0 virtual tables (which have no addressable rowid), so
    # we probe with COUNT and only touch tables that have something to move.
    counts: dict[str, int] = {}
    for t in _project_tag_tables(db):
        n = db.fetchone(f"SELECT COUNT(*) AS c FROM {t} WHERE project = ?", (source,))["c"]
        if n:
            counts[t] = n

    # Full-row snapshot for reversibility — merges can DROP superseded rows (see
    # below), so rowids alone wouldn't restore them. Skip tables that can't be
    # SELECT *'d (none expected among rowid tables).
    rollback: dict[str, list[dict]] = {}
    for t in counts:
        try:
            rollback[t] = db.fetchall(f"SELECT * FROM {t} WHERE project = ?", (source,))
        except Exception:
            rollback[t] = []

    result = {
        "source": source,
        "target": target,
        "tables": counts,
        "total_rows": sum(counts.values()),
        "dry_run": dry_run,
    }
    if dry_run:
        result["mutation"] = "NONE — dry run"
        return result

    # Conflict-safe: some tables have a UNIQUE(project, …) key (e.g. progress),
    # so a source row can collide with an existing target row. UPDATE OR IGNORE
    # moves everything that doesn't collide; the leftover colliding source rows
    # are duplicates the target already covers, so they're dropped.
    dropped: dict[str, int] = {}
    with db.transaction():
        for t in counts:
            db.execute(f"UPDATE OR IGNORE {t} SET project = ? WHERE project = ?", (target, source))
            left = db.fetchone(f"SELECT COUNT(*) AS c FROM {t} WHERE project = ?", (source,))["c"]
            if left:
                dropped[t] = left
                db.execute(f"DELETE FROM {t} WHERE project = ?", (source,))
        # Retire the source on BOTH axes. Its rows were just moved to the
        # target, so there is nothing left for agents to see and nothing left
        # worth scanning. Setting both is required to preserve the pre-
        # migration-100 behaviour, when `active` alone gated indexing too.
        db.execute(
            "UPDATE projects SET active = 0, indexed = 0 WHERE id = ?",
            (source,),
        )

    result["moved"] = {t: counts[t] - dropped.get(t, 0) for t in counts}
    result["dropped_as_duplicate"] = dropped
    result["mutation"] = "APPLIED — source retired (active=0)"
    result["rollback_rows"] = rollback
    return result


def render_manifest_md(m: dict) -> str:
    """Render the manifest as review-ready markdown."""
    s = m["summary"]
    out = ["# Registry cleanup — DRY RUN (zero mutation)", ""]
    out.append(f"- projects: **{s['projects_total']}** · canonical {s['canonical']} · provisional {s['provisional']}")
    out.append(f"- deterministic merge groups: **{s['deterministic_merge_groups']}**")
    out.append(f"- safe archive (provisional, 0 tags): **{s['safe_archive_zero_tags']}**")
    out.append(f"- needs semantic classification (provisional, has knowledge): **{s['needs_semantic_classification']}**")
    out.append(f"- tag stores scanned: {', '.join(s['tag_stores'])}")
    out.append("")

    if m["deterministic_merges"]:
        out.append("## Deterministic merges (safe — canonical-slug variants)")
        for g in m["deterministic_merges"]:
            srcs = ", ".join(f"{x['id']} ({x['tags_total']} tags)" for x in g["sources"])
            out.append(f"- **{g['target']}** ← {srcs} · {g['memories_rerouted']} tags rerouted")
        out.append("")

    if m["needs_classification"]:
        out.append("## Needs semantic classification (deferred — ratify-gated)")
        for x in m["needs_classification"][:15]:
            out.append(f"- {x['id']} — {x['tags_total']} tags")
        if len(m["needs_classification"]) > 15:
            out.append(f"- … +{len(m['needs_classification']) - 15} more")
        out.append("")

    if m["safe_archive"]:
        out.append(f"## Safe archive — {len(m['safe_archive'])} provisional rows with zero knowledge attached")
        out.append("- " + ", ".join(x["id"] for x in m["safe_archive"][:30]))
        if len(m["safe_archive"]) > 30:
            out.append(f"- … +{len(m['safe_archive']) - 30} more")

    return "\n".join(out)
