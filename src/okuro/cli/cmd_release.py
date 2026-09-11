# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro release — stage, gate and commit a release-by-export. Never pushes.
# index:
#   imports
#   def _print_results
#   def release
#   def release_export
#   def release_gates
#   def release_commit_cmd
#   def release_bump
#   def release_owner_baseline
# AGENT_HEADER_END -->
"""okuro release — the public repo is GENERATED, never pushed from here.

    okuro release export  --out DIR [--ref HEAD] [--no-smoke]
    okuro release gates   DIR [--no-smoke]
    okuro release commit  --export DIR --release-repo DIR
    okuro release bump    X.Y.Z [--repo DIR]
    okuro release owner-baseline EXPORT_DIR

``bump`` is the ONE writer of the version: pyproject.toml is the source of
truth and every other site (``__init__``, README badge, frontend package)
is rewritten from the same argument — see okuro.release.version.
``owner-baseline`` re-pins the owner-residue ratchet from a measured export;
the gate only ever lets that number fall.

``commit`` re-runs EVERY gate (smoke included, not skippable) before it
writes the synthetic append-only commit. The push that follows is a human
act: push-audit protocol + explicit approval.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import click

from .output import console, ok, fail, heading


def _print_results(results) -> int:
    total = 0
    for gate, findings in results.items():
        if findings:
            fail(f"{gate}: {len(findings)} finding(s)")
            for f in findings[:25]:
                console.print(f"    {f.path} — {f.detail}")
            if len(findings) > 25:
                console.print(f"    … and {len(findings) - 25} more")
            total += len(findings)
        else:
            ok(f"{gate}: clean")
    return total


def _version(export_dir: Path) -> str:
    """The SSOT value — the same site okuro.release.version.SSOT names."""
    pyproject = export_dir / "pyproject.toml"
    with open(pyproject, "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


@click.group()
def release():
    """Generate and verify the public release repo (release-by-export)."""


@release.command("export")
@click.option("--out", required=True, type=click.Path(path_type=Path),
              help="Destination directory for the staged export (must be empty).")
@click.option("--ref", default="HEAD", show_default=True,
              help="Committed ref to export — the object DB is read, never the worktree.")
@click.option("--repo", default=".", type=click.Path(path_type=Path),
              help="Dev repo root.")
@click.option("--no-smoke", is_flag=True,
              help="Skip only the install-smoke gate (iteration aid; "
                   "`release commit` will refuse such a verdict).")
def release_export(out: Path, ref: str, repo: Path, no_smoke: bool):
    """Stage the manifest surface of REF into OUT, then run every gate."""
    from okuro.release.export import ExportError, stage_export
    from okuro.release.gates import run_gates, verdict

    heading("release export")
    try:
        result = stage_export(repo, out, ref)
    except ExportError as exc:
        fail(str(exc))
        raise SystemExit(1)
    ok(f"staged {len(result.files)} files from {result.source_sha[:12]} → {out}")

    results = run_gates(out, smoke=not no_smoke)
    findings = _print_results(results)
    if verdict(results, smoke_ran=not no_smoke):
        ok(f"ALL GATES PASS — exportable. Source sha {result.source_sha[:12]}")
        return
    if no_smoke and findings == 0:
        fail("gates clean but install-smoke was SKIPPED — not a committable verdict")
    else:
        if no_smoke:
            console.print("  (install-smoke was skipped — this verdict cannot be committed)")
        fail(f"{findings} finding(s) — this export must not leave the machine")
    raise SystemExit(1)


@release.command("gates")
@click.argument("export_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--no-smoke", is_flag=True, help="Skip only the install-smoke gate.")
def release_gates(export_dir: Path, no_smoke: bool):
    """Re-run the exit gates over an already-staged export."""
    from okuro.release.gates import run_gates, verdict

    heading("release gates")
    results = run_gates(export_dir, smoke=not no_smoke)
    findings = _print_results(results)
    if verdict(results, smoke_ran=not no_smoke):
        ok("ALL GATES PASS")
        return
    fail(f"{findings} finding(s)" + (" (smoke skipped)" if no_smoke else ""))
    raise SystemExit(1)


@release.command("commit")
@click.option("--export", "export_dir", required=True,
              type=click.Path(exists=True, path_type=Path),
              help="A staged export directory (from `okuro release export`).")
@click.option("--release-repo", required=True,
              type=click.Path(exists=True, path_type=Path),
              help="Local clone of the release repo.")
@click.option("--source-sha", required=True,
              help="The dev-repo sha this export was staged from (recorded in the commit).")
def release_commit_cmd(export_dir: Path, release_repo: Path, source_sha: str):
    """Gate (fully — smoke not skippable), then write the append-only release commit.

    Does NOT push. Push-audit protocol + explicit approval come first.
    """
    from okuro.release.gates import run_gates, verdict
    from okuro.release.publish import PublishError, release_commit

    heading("release commit")
    results = run_gates(export_dir, smoke=True)
    if not verdict(results, smoke_ran=True):
        _print_results(results)
        fail("gates failed — refusing to commit")
        raise SystemExit(1)
    ok("all gates pass")

    try:
        sha = release_commit(
            export_dir, release_repo,
            source_sha=source_sha, version=_version(Path(export_dir)),
        )
    except PublishError as exc:
        fail(str(exc))
        raise SystemExit(1)
    ok(f"release commit {sha[:12]} written to {release_repo}")
    console.print("  NOT pushed. Next: push-audit protocol, then explicit approval.")


@release.command("bump")
@click.argument("new_version")
@click.option("--repo", default=".", type=click.Path(exists=True, path_type=Path),
              help="Dev repo root.")
def release_bump(new_version: str, repo: Path):
    """Set the version at EVERY site from one argument (pyproject is the SSOT).

    Rewrites only the version string inside each site; nothing else moves.
    Commit the result yourself — the bump is a write, not a release.
    """
    from okuro.release.version import SITES, bump, read_versions

    heading("release bump")
    try:
        changed = bump(repo, new_version)
    except (ValueError, FileNotFoundError) as exc:
        fail(str(exc))
        raise SystemExit(1)
    for site in SITES:
        mark = "rewritten" if site.path in changed else "already"
        console.print(f"    {site.path} — {mark} {new_version}")
    after = set(read_versions(repo).values())
    if after != {new_version}:
        fail(f"sites still disagree after bump: {sorted(map(str, after))}")
        raise SystemExit(1)
    ok(f"{len(changed)} site(s) rewritten; every site now states {new_version}")


@release.command("owner-baseline")
@click.argument("export_dir", type=click.Path(exists=True, path_type=Path))
def release_owner_baseline(export_dir: Path):
    """Re-pin the owner-residue ratchet from a staged export.

    Writes src/okuro/release/owner_baseline.json (dev-only; the manifest
    excludes it). Refuses to RAISE the pinned count — the ratchet only
    turns one way; a rise is a leak to fix, not a number to accept.
    """
    from okuro.release.gates import owner_findings, read_owner_baseline, write_owner_baseline

    heading("release owner-baseline")
    findings = owner_findings(export_dir)
    current = read_owner_baseline()
    if current is not None and len(findings) > current["total"]:
        fail(f"{len(findings)} owner finding(s) > pinned {current['total']} — "
             "the ratchet does not turn that way; sweep the new residue instead")
        for f in findings:
            console.print(f"    {f.path} — {f.detail}")
        raise SystemExit(1)
    path = write_owner_baseline(findings)
    ok(f"pinned {len(findings)} owner finding(s) in {path}")
