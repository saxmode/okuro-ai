# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro codegraph — Tree-sitter ingest, layer classify, diff-impact ripple from KG.
# index:
#   imports
#   def codegraph
#   def ingest
#   def diff_impact
#   def _git_changed_files
#   def _resolve_project
# AGENT_HEADER_END -->
"""okuro codegraph — code-graph ingestion + diff-impact ripple.

Subcommands:
  okuro codegraph ingest [path] [--project SLUG] [--llm-layers]
  okuro codegraph diff-impact [--base BRANCH] [--depth N] [--project SLUG]
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

import click

from .output import console, ok, warn, info, heading, data_table


@click.group()
def codegraph():
    """Tree-sitter code-graph ingestion + diff-impact analysis."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_project(slug: str | None, root: Path) -> str:
    if slug:
        return slug
    # Fall back to the registered cortex root if any; else the dir name.
    try:
        from okuro.cortex.roots import project_for_path

        guess = project_for_path(root)
        if guess:
            return guess
    except Exception:
        pass
    return root.resolve().name


def _git_changed_files(root: Path, base: str) -> list[Path]:
    """Files changed between ``base`` and the working tree (committed + index + untracked)."""
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", base],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []

    names: set[str] = set()
    if diff.returncode == 0:
        names.update(line for line in diff.stdout.strip().split("\n") if line)
    if untracked.returncode == 0:
        names.update(line for line in untracked.stdout.strip().split("\n") if line)
    # Drop codegraph's own bookkeeping — touching the marker is not a code change.
    names.discard(CODEGRAPH_MARKER)
    return [root / n for n in sorted(names)]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


CODEGRAPH_MARKER = ".okuro-codegraph-marker"


def _read_marker(root: Path) -> str | None:
    f = root / CODEGRAPH_MARKER
    if not f.exists():
        return None
    return f.read_text().strip() or None


def _write_marker(root: Path, commit: str) -> None:
    (root / CODEGRAPH_MARKER).write_text(commit)


@codegraph.command("ingest")
@click.argument("path", type=click.Path(exists=True, file_okay=False, dir_okay=True), default=".")
@click.option("--project", default=None, help="Project slug (defaults to directory name).")
@click.option("--llm-layers/--no-llm-layers", default=False,
              help="Send unknown-layer files to the LLM (requires a configured bridge provider).")
@click.option("--full/--incremental", default=False,
              help="Force a full re-walk. Incremental (default) only re-parses git-changed files since the last ingest.")
def ingest(path, project, llm_layers, full):
    """Parse a project with Tree-sitter, classify layers, write KG triples."""
    from okuro.cortex.codegraph import (
        ingest_project as ts_ingest,
        discover_project_files,
        classify_project,
        write_layers_to_kg,
    )
    from okuro.cortex.scanner import GitIntegration
    from okuro.sense.kg_code import ingest_code_facts

    root = Path(path).resolve()
    slug = _resolve_project(project, root)
    heading(f"codegraph ingest — {root} (project={slug})")

    # Pass 1: cheap path walk (no parsing) — gives us the project file
    # set up front so pass 2 can stream facts without materializing AND
    # so the import resolver sees every project file regardless of which
    # ones we actually re-parse this run.
    project_files = discover_project_files(root)
    info(f"discovered {len(project_files)} parseable files")
    if not project_files:
        warn("no parseable files discovered")
        return

    # Incremental: ask git for changes since the last marker. Falls back
    # to full when no git repo, no marker, or --full given.
    git = GitIntegration(root)
    last_commit = _read_marker(root)
    paths_to_parse: list[Path] | None = None
    if not full and git.is_git_repo() and last_commit:
        changed_full = git.get_changed_files(last_commit)
        changed_in_project = [
            p for p in changed_full
            if str(p.relative_to(root)) in project_files
        ] if changed_full else []
        paths_to_parse = changed_in_project
        info(
            f"incremental: {len(paths_to_parse)} changed files since "
            f"{last_commit[:8]}"
        )

    # Pass 2: stream Tree-sitter parsing + KG ingest.
    # prune_missing only when we walked the full project — otherwise an
    # incremental run would wrongly orphan everything we didn't re-parse.
    full_run = paths_to_parse is None
    facts_iter = ts_ingest(root) if full_run else ts_ingest(root, paths=paths_to_parse)
    stats = ingest_code_facts(
        facts_iter,
        project=slug,
        root=root,
        prune_missing=full_run,
        project_files=project_files,
    )
    ok(
        f"KG: {stats['files_ingested']} ingested, "
        f"{stats['files_skipped']} unchanged, "
        f"{stats['triples_inserted']} triples inserted, "
        f"{stats['triples_invalidated']} stale closed, "
        f"{stats['orphans_closed']} files orphaned"
    )
    if stats["parse_failures"]:
        warn(
            f"{stats['parse_failures']} parse failures — run "
            f"`okuro codegraph doctor` for details"
        )

    layers = classify_project(root, sorted(project_files), use_llm_fallback=llm_layers)
    n_layer = write_layers_to_kg(layers, project=slug)
    distribution = defaultdict(int)
    for layer in layers.values():
        distribution[layer] += 1
    ok(f"layers: {n_layer} files tagged")

    # Edge resolution + provenance tiers — turn provisional cross-file calls /
    # inherits into extracted / inferred / ambiguous / external with
    # import-evidence promotion. Runs after ingest so it sees the whole
    # project's symbols. See okuro.cortex.codegraph.tiers.
    from okuro.sense.kg_code import resolve_edges
    tiers = resolve_edges(slug)
    if tiers["resolved"] or tiers["reconfidenced"]:
        ok(
            f"tiers: {tiers['extracted']} extracted, {tiers['inferred']} inferred, "
            f"{tiers['ambiguous']} ambiguous, {tiers['external']} external "
            f"({tiers['resolved']} resolved, {tiers['reconfidenced']} retiered)"
        )
        if tiers["ambiguous"]:
            warn(
                f"{tiers['ambiguous']} ambiguous edges — run "
                f"`okuro codegraph audit --project {slug}` to review"
            )

    # Subsystem detection (Louvain over the file import graph) — harvested from
    # Graphify. Persists in_community triples so /knowledge colors subsystems.
    from okuro.cortex.codegraph import write_communities_to_kg
    n_comm = write_communities_to_kg(slug)
    if n_comm:
        ok(f"communities: {n_comm} files partitioned")

    # Update marker only on full or successful incremental runs in a git
    # repo. Reuses GitIntegration's current-commit lookup so dirty trees
    # still get a usable marker (HEAD commit; the diff next time picks up
    # uncommitted churn via 'git diff --name-only' + ls-files --others).
    if git.is_git_repo():
        commit = git.get_current_commit()
        if commit:
            _write_marker(root, commit)
    rows = sorted(
        [[layer, str(count)] for layer, count in distribution.items()],
        key=lambda r: -int(r[1]),
    )
    console.print(data_table(["layer", "files"], rows, title="layer distribution"))


@codegraph.command("doctor")
@click.argument("path", type=click.Path(exists=True, file_okay=False, dir_okay=True), default=".")
def doctor(path):
    """Re-parse the project and list every file Tree-sitter failed on."""
    from okuro.cortex.codegraph import ingest_project as ts_ingest

    root = Path(path).resolve()
    heading(f"codegraph doctor — {root}")

    failures = []
    parsed = 0
    for facts in ts_ingest(root):
        parsed += 1
        if facts.error and "unsupported" not in facts.error:
            try:
                rel = str(Path(facts.file).relative_to(root))
            except ValueError:
                rel = facts.file
            failures.append([rel, facts.language, facts.error])

    info(f"parsed {parsed} files")
    if not failures:
        ok("no parse failures")
        return

    fail_count = len(failures)
    console.print(
        data_table(
            ["file", "language", "error"],
            failures,
            title=f"parse failures ({fail_count})",
        )
    )


@codegraph.command("audit")
@click.option("--project", required=True, help="Project slug.")
@click.option("--top-n", default=15, show_default=True, type=int,
              help="How many god nodes / ambiguous edges to list.")
@click.option("--print-json/--table", default=False,
              help="Print the raw audit JSON instead of the table view.")
def audit(project, top_n, print_json):
    """Index self-audit: provenance tiers, ambiguity, coverage, god nodes.

    okuro's GRAPH_REPORT — how deterministic the code index is and where it is
    guessing, so an approximate answer is never mistaken for a precise one.
    """
    from okuro.cortex.codegraph import index_audit

    report = index_audit(project, top_n=top_n)
    heading(f"codegraph audit — project={project} ({report['files']} files)")

    if print_json:
        import json as _json
        console.print(_json.dumps(report, indent=2))
        return

    det = report["determinism"]
    ratio = det["ratio"]
    ratio_str = f"{ratio:.0%}" if ratio is not None else "—"
    info(
        f"determinism: {ratio_str} of resolved call/inherit edges are "
        f"import-proven ({det['extracted']} extracted / {det['inferred']} inferred "
        f"/ {det['ambiguous']} ambiguous)"
    )
    if report["unclassified_triples"]:
        warn(
            f"{report['unclassified_triples']} legacy untiered triples — "
            f"re-run `codegraph ingest --full` to tier them"
        )

    tier_rows = [
        [pred, str(counts.get("extracted", 0)), str(counts.get("inferred", 0)),
         str(counts.get("ambiguous", 0)), str(counts.get("external", 0))]
        for pred, counts in report["edge_tiers"].items()
    ]
    console.print(data_table(
        ["predicate", "extracted", "inferred", "ambiguous", "external"],
        tier_rows, title="edge provenance tiers",
    ))

    cov = report["language_coverage"]
    if cov["degraded_sha_only"]:
        deg = ", ".join(f"{ext}×{n}" for ext, n in cov["degraded_sha_only"].items())
        warn(f"degraded (sha-only, no symbol extraction): {deg}")

    if report["ambiguous"]:
        amb_rows = [
            [e["from"], e["name"], str(len(e["candidates"])),
             ", ".join(c.split("::", 1)[-1] for c in e["candidates"][:3])]
            for e in report["ambiguous"]
        ]
        console.print(data_table(
            ["from", "name", "#cands", "candidates"],
            amb_rows, title=f"ambiguous edges ({len(report['ambiguous'])})",
        ))
    else:
        ok("no ambiguous edges")

    if report["god_nodes"]:
        god_rows = [
            [g["node"], str(g["degree"]), str(g["in"]), str(g["out"]),
             g.get("layer") or "—"]
            for g in report["god_nodes"]
        ]
        console.print(data_table(
            ["node", "degree", "in", "out", "layer"],
            god_rows, title="god nodes (widest ripple)",
        ))

    xr = report["cross_repo"]
    if xr["total"]:
        tier_str = ", ".join(f"{t}×{n}" for t, n in xr["tiers"].items())
        info(f"cross-repo: {xr['total']} bridges touch this repo ({tier_str})")
        xr_rows = [
            [b["anchor"], b["kind"], b["tier"], b["role"],
             ", ".join(b["linked"][:3])]
            for b in xr["bridges"]
        ]
        console.print(data_table(
            ["anchor", "kind", "tier", "role", "linked repos"],
            xr_rows, title="cross-repo bridges",
        ))


@codegraph.command("tour")
@click.option("--project", required=True, help="Project slug.")
@click.option("--max-steps", default=20, show_default=True, type=int)
@click.option("--focus-layer", default=None,
              help="Narrow the tour to a single layer (e.g. 'api').")
@click.option("--start-from", default=None,
              help="Anchor this file as step 1 regardless of layer order.")
@click.option("--print-only/--save", default=False,
              help="Print the JSON body instead of writing an artifact.")
def tour(project, max_steps, focus_layer, start_from, print_only):
    """Generate a dependency-ordered code-tour from the KG.

    Reads in_layer + defines + imports triples for the project and emits a
    single ``tour: <project>`` report artifact that the /knowledge
    TourPanel renders. Deterministic — no LLM round-trip. The tour-builder
    role YAML is the LLM-driven alternative when phrasing matters more
    than determinism.
    """
    from okuro.cortex.codegraph.tour import build_tour, emit_tour_artifact

    body = build_tour(
        project=project,
        max_steps=max_steps,
        focus_layer=focus_layer,
        start_from=start_from,
    )

    heading(f"codegraph tour — project={project} ({len(body['steps'])} steps)")

    if not body["steps"]:
        warn("no in_layer triples found — run `codegraph ingest` first")
        return

    rows = [
        [str(s["index"]), s["file"], s["layer"],
         ", ".join(s["symbols"][:2]) or "—",
         ", ".join(map(str, s["prerequisites"])) or "—"]
        for s in body["steps"]
    ]
    console.print(
        data_table(
            ["#", "file", "layer", "symbols", "after"],
            rows,
            title="tour preview",
        )
    )

    if print_only:
        import json as _json
        console.print(_json.dumps(body, indent=2))
        return

    artifact_id = emit_tour_artifact(body, project=project)
    if isinstance(artifact_id, str) and artifact_id.startswith("REJECTED"):
        from .output import fail
        fail(artifact_id)
        return
    ok("tour artifact written — visible at /knowledge (Graph tab → tour button)")


@codegraph.command("diff-impact")
@click.option("--base", default="HEAD", show_default=True,
              help="Git revision to diff against (e.g. main, HEAD~1, origin/main).")
@click.option("--depth", default=3, show_default=True, type=int,
              help="BFS depth limit for ripple analysis.")
@click.option("--project", default=None, help="Project slug (defaults to directory name).")
@click.option("--path", default=".", type=click.Path(exists=True, file_okay=False, dir_okay=True),
              help="Repo root.")
@click.option("--predicate", "predicates", multiple=True,
              default=("imports", "calls", "defines"), show_default=True,
              help="KG predicates to follow. Repeatable.")
def diff_impact(base, depth, project, path, predicates):
    """Show files (grouped by layer) impacted by the current diff vs BASE.

    Uses the code-graph KG: for each changed file, BFS along the chosen
    predicates in the 'incoming' direction (who depends on me?). Group
    results by layer and print a one-screen summary table.
    """
    from okuro.sense.kg_code import ripple

    root = Path(path).resolve()
    slug = _resolve_project(project, root)
    heading(f"diff-impact — {root} vs {base} (project={slug}, depth={depth})")

    changed = _git_changed_files(root, base)
    if not changed:
        warn(f"no changes detected against {base}")
        return

    # Show what's changed up front
    rel_changed = []
    for p in changed:
        try:
            rel_changed.append(str(p.relative_to(root)))
        except ValueError:
            continue
    info(f"{len(rel_changed)} changed files")
    for rel in rel_changed[:15]:
        info(f"  {rel}")
    if len(rel_changed) > 15:
        info(f"  ... and {len(rel_changed) - 15} more")

    # Collect ripples + layer of each impacted entity
    impact: dict[str, dict] = {}
    file_layers = _file_layers(slug)

    for rel in rel_changed:
        results = ripple(
            rel,
            predicates=tuple(predicates),
            direction="incoming",
            max_depth=depth,
            project=slug,
        )
        for r in results:
            subject = r["subject"]
            # If subject is a symbol (file::name), report the file
            file = subject.split("::", 1)[0]
            existing = impact.get(file)
            depth_val = r["depth"]
            if existing is None or depth_val < existing["min_depth"]:
                impact[file] = {
                    "min_depth": depth_val,
                    "layer": file_layers.get(file, "unknown"),
                    "via": rel,
                }

    # Drop the changed files themselves from the impact set — they're the source
    for rel in rel_changed:
        impact.pop(rel, None)

    if not impact:
        warn("no downstream impact found in the KG (did you run `codegraph ingest` first?)")
        return

    # Group by layer for a one-screen summary
    by_layer = defaultdict(list)
    for file, meta in impact.items():
        by_layer[meta["layer"]].append((file, meta))

    rows = []
    for layer in sorted(by_layer):
        items = sorted(by_layer[layer], key=lambda x: (x[1]["min_depth"], x[0]))
        for file, meta in items:
            rows.append([
                layer,
                file,
                str(meta["min_depth"]),
                meta["via"],
            ])

    heading(f"\nimpacted files: {len(impact)} (across {len(by_layer)} layers)")
    console.print(
        data_table(
            ["layer", "file", "depth", "via"],
            rows,
            title="downstream impact",
        )
    )
    # Tight summary by layer
    summary_rows = sorted(
        [[layer, str(len(files))] for layer, files in by_layer.items()],
        key=lambda r: -int(r[1]),
    )
    console.print(data_table(["layer", "impacted"], summary_rows, title="by layer"))


def _file_layers(project: str | None) -> dict[str, str]:
    """Return ``{relpath: layer}`` from the most-recent ``in_layer`` triples."""
    from okuro.db import get_db

    db = get_db()
    where = "predicate = 'in_layer' AND (valid_to IS NULL OR valid_to = '')"
    params: tuple = ()
    if project:
        where += " AND (project = ? OR project IS NULL)"
        params = (project,)
    rows = db.fetchall(
        f"SELECT subject, object FROM kg_triples WHERE {where}", params
    )
    return {r["subject"]: r["object"] for r in rows}
