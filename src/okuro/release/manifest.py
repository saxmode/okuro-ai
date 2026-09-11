# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: release manifest — the path allowlist defining okuro's public surface.
# index:
#   RELEASE_REPO_URL / DEV_SLUG
#   MANIFEST_DIRS / MANIFEST_FILES / SCRIPTS_FILES
#   EXCLUDE_GLOBS
#   BINARY_ALLOWLIST / REQUIRED_FILES
#   OWNER_BASELINE
#   README_BANNER
#   def exclusion_spec
#   def selects
# AGENT_HEADER_END -->
"""What the release repo serves — an ALLOWLIST, never a denylist.

Same reasoning as PRODUCT_ROOT_ENTRIES in cmd_git_guards.py, one layer
further out: the guard decides what may enter the DEV tree, this manifest
decides what leaves it. Every top-level entry here must be a member of
PRODUCT_ROOT_ENTRIES — tests/release/test_manifest.py pins that, so the
two lists cannot drift apart.

Deliberately NOT shipped even though tracked as product in the dev repo:

  .github/      dev CI (repo-hygiene, windows-port) would run red on a
                generated repo that ships no tests/ — and the release repo
                takes no PRs, so CI has nothing to protect there.
  AGENTS.md     instructions for agents working in the DEV tree (guards,
                worktrees, deploy loop) — meaningless and misleading in a
                generated repo.
  tests/ + every test file under src/ — the export contract requires ZERO
                test files (frontend tests inside src/ carry customer
                tokens; some are already on the old public origin).
  docs/research/**  carries the owner's profile and raw session data.
  scripts/ (all but the two the installer execs) — the rest is the
                owner's benchmark/backfill workbench.
  installer/    the macOS .pkg build (unsigned, failed on real hardware
                2026-06-26, never hosted). Retired 2026-09-11: any
                double-clickable on macOS needs a Developer ID, so macOS
                installs the same way Linux does — bootstrap.sh over HTTPS,
                then install.sh. One entry point per platform, no artifact
                to sign.
"""

from __future__ import annotations

import pathspec

#: The public installer clones this. bootstrap.sh:25 hardcodes it; the
#: installer-URL gate VERIFIES the two agree rather than rewriting.
RELEASE_REPO_URL = "https://github.com/saxmode/okuro-ai.git"

#: The private dev repo's slug — must appear NOWHERE in an export.
#: Derived, not literal: this module ships in the export, so a literal
#: here would be the dev-slug gate's first (self-inflicted) finding.
DEV_SLUG = "okuro-ai" + "-" + "dev"

#: Directories that ship wholesale (minus EXCLUDE_GLOBS below).
#: docs/ is deliberately absent: everything under it today (analysis,
#: codebase-intelligence, research) is dev-internal and names customer
#: projects — add it back the day a public doc exists.
MANIFEST_DIRS: tuple[str, ...] = ("assets", "src")

#: Top-level files that ship. Explicit entries for the legal/required set
#: (LICENSE, NOTICE, SECURITY.md, README.md) per the Phase-0 contract —
#: NOTICE has silently dropped off origin once already.
MANIFEST_FILES: tuple[str, ...] = (
    ".gitignore", "LICENSE", "NOTICE", "README.md", "SECURITY.md",
    "bootstrap.ps1", "bootstrap.sh", "hatch_build.py",
    "install.ps1", "install.sh", "o-kuro.svg", "pyproject.toml",
    "requirements-lock.txt",
)

#: File-level allowlist inside scripts/ — exactly what the install chain
#: execs (install.sh:80,88,745 routes to update.sh and build-frontend.sh).
#: Nothing else in scripts/ is load-bearing post-clone; the directory also
#: holds the historically worst token-carrying file, so it stays file-level.
SCRIPTS_FILES: tuple[str, ...] = (
    "scripts/update.sh",
    "scripts/build-frontend.sh",
)

#: Gitwildmatch exclusions applied INSIDE the shipped directories.
EXCLUDE_GLOBS: tuple[str, ...] = (
    # zero test files in the export — asserted again by a gate
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/__tests__/**",
    # the prism kit's proof/calibration harness — dev tooling that happens
    # to live in-package; nothing imports it at runtime (one comment ref)
    "src/okuro/prism/kit/tests/**",
    # the owner-residue ratchet — a dev-side measurement of the export,
    # meaningless (and mildly revealing: it names files) in the export
    "src/okuro/release/owner_baseline.json",
)

#: Every binary in an export must be one of these, byte-sniffed at gate
#: time. Enumerated, not globbed: a NEW binary is exactly the event the
#: gate exists to surface (binaries defeat text scans — proven twice).
BINARY_ALLOWLIST: tuple[str, ...] = (
    "src/okuro/web/frontend/public/icons/icon-192.png",
    "src/okuro/web/frontend/public/icons/icon-512.png",
)

#: Presence-asserted by a gate; absence blocks the release.
REQUIRED_FILES: tuple[str, ...] = ("LICENSE", "NOTICE", "README.md", "SECURITY.md")

#: The owner-residue ratchet (gates.gate_owner): a committed count the gate
#: only lets fall. Lives next to the gate so it is found in any install
#: mode; excluded above so it never ships; gate_owner asserts both.
OWNER_BASELINE = "src/okuro/release/owner_baseline.json"

#: Prepended to README.md at stage time. Contribution policy day one:
#: issues-only, PRs disabled. Attribution line pending an open decision
#: (pin identity.author vs profile-driven) — add nothing until decided.
README_BANNER = """\
> **This is a generated repository.** It is exported from a private
> development tree by an automated release pipeline; its history is
> append-only (one commit per release) and pull requests are not
> accepted — they would be destroyed by the next export.
> Bug reports and feature requests are welcome as **issues**.

"""


def exclusion_spec() -> pathspec.PathSpec:
    """The EXCLUDE_GLOBS compiled in .gitignore dialect."""
    return pathspec.PathSpec.from_lines("gitignore", EXCLUDE_GLOBS)


def selects(relpath: str, *, _spec_cache: list = []) -> bool:
    """Does the manifest ship this repo-relative path?

    Top-level files by name, manifest directories wholesale, scripts/ by
    file-level allowlist — then the exclusion globs veto. Everything not
    named is out: an allowlist does not have to predict the next scratch
    directory.
    """
    if not _spec_cache:
        _spec_cache.append(exclusion_spec())
    top = relpath.split("/", 1)[0]
    selected = (
        relpath in MANIFEST_FILES
        or relpath in SCRIPTS_FILES
        or (top in MANIFEST_DIRS and "/" in relpath)
    )
    return selected and not _spec_cache[0].match_file(relpath)
