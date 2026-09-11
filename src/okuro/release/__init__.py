# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: release-by-export pipeline — generate okuro's public repo from the private dev tree.
# index:
#   (package marker only — see manifest.py, export.py, gates.py, publish.py)
# AGENT_HEADER_END -->
"""Release-by-export: the dev repo stays private forever; the public repo
is GENERATED from a path allowlist with blocking exit gates.

The pipeline has three stages, each its own module:

  manifest.py   WHAT ships — path allowlist (pinned ⊆ PRODUCT_ROOT_ENTRIES
                by test), glob exclusions, binary allowlist, required files.
  export.py     STAGE — materialize the manifest surface of one committed
                sha into a directory. Refuses a dirty tree.
  gates.py      VERIFY — blocking exit gates over the staged tree: content
                token scan, binary enumeration, dev-slug absence, zero test
                files, required files, installer URL, install smoke.
  publish.py    COMMIT — one synthetic append-only commit per release into
                the release repo clone. Parentage is asserted, history is
                never re-rooted, and NOTHING here pushes.

Pushing is deliberately absent from this package: a release push is a
human act (push-audit protocol + the owner's explicit approval).
"""
