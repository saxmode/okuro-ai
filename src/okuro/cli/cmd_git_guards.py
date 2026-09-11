# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro setup-git-guards — install provider-agnostic branch-integrity hooks.
# index:
#   imports
#   PRODUCT_ROOT_ENTRIES
#   _PRELUDE
#   PRE_COMMIT_HOOK
#   PRE_PUSH_HOOK
#   POST_CHECKOUT_HOOK
#   SHELL_WRAPPER_SNIPPET
#   HOOKS
#   def _is_git_repo
#   def _hooks_dir
#   def _worktree_roots
#   def _backfill_bindings
#   def _registered_repos
#   def _write_hook
#   def setup_git_guards
# AGENT_HEADER_END -->
"""okuro setup-git-guards — install provider-agnostic branch-integrity hooks.

Hard-won convention (memory + ORCH-PARALLEL-TREE principle): multiple
concurrent agent sessions editing the same repo ping-pong branch state,
so the MCP stdio subprocess each session spawns silently loads the wrong
code — and agents commit into each other's branches.

Enforcement lives in git hooks rather than in per-provider instruction
files because every provider (Claude Code, Codex, Gemini, Cursor, cron,
a human shell) ultimately shells out to ``git``. The hook fires in that
subprocess regardless of who spawned it. Instructions are advisory;
hooks are not.

Eight invariants, three hook files:

  PIN      main worktree may only commit to ``main``        (pre-commit)
  BIND     a sibling worktree may only commit to the branch
           recorded in its ``.okuro-worktree`` file          (pre-commit)
  SCOPE    denylisted paths may never be newly added, and
           the repo ROOT is an allowlist of okuro the product (pre-commit)
  CONTENT  staged additions may not carry real brand/person
           tokens (list in ~/.okuro/guard/, okuro repo only)  (pre-commit)
  SECRET   staged additions may not carry a credential --
           vendor-prefixed keys, PEM private keys, or a
           secret-named variable holding a long literal      (pre-commit)
  ADOPT    agent-authored files older than HEAD may not be
           silently committed by someone who did not write
           them                                              (pre-commit)
  PROTECT  no non-fast-forward push or deletion of ``main``  (pre-push)
  PUBLISH  a push may not make non-product paths public, and
           may not carry an ``--all``-shaped pile of refs     (pre-push)

PUBLISH is the only invariant that is range- and destination-aware. A
commit-time guard sees one staged change against one tree; a push hands
over an entire range of commits to a named remote, and after 2026-08-04
that remote is public. Both facts are knowable at pre-push and nowhere
else, which is why this is its own invariant rather than more SCOPE.

``post-checkout`` stays advisory — it warns on branch state that the
commit-time guards will later refuse, so the feedback arrives before the
work rather than after it.

Branch binding is recorded in a ``.okuro-worktree`` file at the worktree
root rather than an environment variable. Env propagation is the one
layer that genuinely differs across providers — MCP stdio subprocesses,
spawn flags and hook configs each strip or reshape it. A file read from
the worktree root makes no assumption about who spawned the shell.
"""

from pathlib import Path
import stat
import subprocess

import click

from .output import console, ok, warn, fail, heading, info


# Chain to a pre-existing foreign hook (git-lfs, husky, ...) that
# `setup-git-guards` displaced. MUST run before any early `exit 0` in the
# okuro hook: git-lfs post-checkout, for one, has to fire on file
# checkouts that okuro's own logic ignores. A non-zero exit from the
# chained hook aborts the operation exactly as it would have before.
_CHAIN = r"""
if [ -x "$0.okuro-chained" ]; then
    "$0.okuro-chained" "$@" || exit $?
fi
"""


# --------------------------------------------------------------------------
# The base/brand boundary, as a path rule.
#
# okuro ships the base design system and okuro-ds. A user's brands, stacks,
# design systems and personal work are the USER's data and must never be in
# the bundle or reach the remote. That rule held in intent and lost to 880
# commits of agent work, because until 2026-08-04 no boundary existed that a
# machine could check.
#
# ALLOWLIST, not denylist, and only at the repo ROOT. Measured 2026-08-05:
# the tracked root is 22 entries and the set of root components ever added
# across the 2551 unpushed commits is exactly that set plus one stray. A
# denylist has to predict the next scratch directory; an allowlist does not
# have to predict anything, which is the whole difference between fixing the
# instance and fixing the class. `tmp/`, `deliverables/`, `design/`,
# `prism-w5-phasee-proof/`, a loose `blank.html` — all refused by one rule,
# along with whatever tomorrow's session drops there.
#
# Anchored at the root ON PURPOSE. `src/okuro/design/` is the product's own
# design package; a rule matching `design/` anywhere would refuse routine work
# on it within a day and be switched off with --no-verify by evening. A guard
# that cries wolf is worse than no guard.
#
# Adding an entry here is the deliberate act of saying "this is okuro, not
# the owner's workbench". That is exactly the decision that had no home before.
PRODUCT_ROOT_ENTRIES: tuple[str, ...] = (
    # directories
    ".github", "assets", "docs", "installer", "scripts", "src", "tests",
    # files
    ".gitignore", "AGENTS.md", "LICENSE", "NOTICE", "README.md", "SECURITY.md",
    "bootstrap.ps1", "bootstrap.sh", "hatch_build.py", "install.ps1",
    "install.sh", "o-kuro.svg", "pyproject.toml", "requirements-lock.txt",
    # Conventional repo furniture — not present today, but unambiguously
    # product if it ever appears. Pre-allowing these keeps the guard off the
    # path of an ordinary contribution; none of them can carry user data.
    ".dockerignore", ".editorconfig", ".gitattributes", ".pre-commit-config.yaml",
    "CHANGELOG.md", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "Dockerfile",
    "Makefile", "docker-compose.yml",
)

#: Bash `case` alternation compiled from the allowlist above. One source of
#: truth: the hooks and tests/repo/test_no_personal_data_tracked.py both read
#: PRODUCT_ROOT_ENTRIES, so the guard and its test cannot drift apart.
_PRODUCT_ROOTS_CASE = "|".join(PRODUCT_ROOT_ENTRIES)


# Shared bash prelude — resolves tree kind, branch and root. Inlined into
# each hook because hooks must be self-contained (nothing to source).
_PRELUDE = r"""
# --- resolve tree kind ------------------------------------------------
# git-dir == git-common-dir in the MAIN worktree; they differ in every
# sibling created by `git worktree add`.
_gitdir="$(git rev-parse --git-dir 2>/dev/null)" || exit 0
_commondir="$(git rev-parse --git-common-dir 2>/dev/null)"
[ -z "$_gitdir" ] && exit 0
_abs_gitdir="$(cd "$_gitdir" 2>/dev/null && pwd -P)"
_abs_commondir="$(cd "$_commondir" 2>/dev/null && pwd -P)"
_root="$(git rev-parse --show-toplevel 2>/dev/null)"
_branch="$(git symbolic-ref --short HEAD 2>/dev/null || echo "")"
if [ "$_abs_gitdir" = "$_abs_commondir" ]; then _is_main_tree=1; else _is_main_tree=0; fi
"""


PRE_COMMIT_HOOK = r"""#!/usr/bin/env bash
# okuro branch-integrity guard — installed by `okuro setup-git-guards`.
# Enforces PIN + BIND + SCOPE at the commit boundary. Provider-agnostic:
# fires for Claude Code, Codex, Gemini, Cursor, cron and human shells
# alike, because they all shell out to `git commit`.
__CHAIN__
__PRELUDE__
_die() {
    echo >&2 ""
    echo >&2 "════════════════════════════════════════════════════════════════════"
    echo >&2 "  ✗ okuro-git-guard: COMMIT REFUSED — $1"
    echo >&2 "════════════════════════════════════════════════════════════════════"
    echo >&2 ""
    shift
    while [ $# -gt 0 ]; do echo >&2 "  $1"; shift; done
    echo >&2 ""
    exit 1
}

# --- PIN: the main worktree may only ever commit to main --------------
if [ "$_is_main_tree" = "1" ] && [ -n "$_branch" ] && [ "$_branch" != "main" ]; then
    if [ -z "$OKURO_ALLOW_MAIN_COMMIT" ]; then
        _die "PIN — main worktree is on '$_branch', not main" \
            "Repo:   $_root" \
            "Branch: $_branch" \
            "" \
            "The main worktree owns the systemd daemons and the editable venv" \
            "install. Committing feature work here makes every concurrent" \
            "session load code from this branch." \
            "" \
            "Remedy" \
            "  1. Move the work to its own tree:  okuro wt add <topic>" \
            "  2. Restore this tree:              git checkout main" \
            "  3. Intentional? Bypass:            OKURO_ALLOW_MAIN_COMMIT=1 git commit ..."
    fi
fi

# --- BIND: a sibling worktree may only commit to its bound branch -----
# Skipped while git owns HEAD. Rebase, cherry-pick, revert, merge-conflict
# resolution and bisect all DETACH HEAD, so $_branch is empty and any
# comparison against the binding would refuse every `git commit --amend`
# at a rebase stop. Git reattaches HEAD when the sequencer finishes, and
# the next ordinary commit is checked normally.
_seq_in_progress=0
for _m in rebase-merge rebase-apply CHERRY_PICK_HEAD REVERT_HEAD MERGE_HEAD BISECT_LOG; do
    [ -e "$_gitdir/$_m" ] && _seq_in_progress=1
done

if [ "$_is_main_tree" = "0" ] && [ -n "$_branch" ] && [ "$_seq_in_progress" = "0" ] \
   && [ -f "$_root/.okuro-worktree" ]; then
    _bound="$(sed -n 's/^branch=//p' "$_root/.okuro-worktree" | head -1)"
    if [ -n "$_bound" ] && [ "$_bound" != "$_branch" ]; then
        if [ -z "$OKURO_ALLOW_BRANCH_DRIFT" ]; then
            _die "BIND — worktree is bound to '$_bound' but HEAD is '$_branch'" \
                "Tree:   $_root" \
                "Bound:  $_bound" \
                "HEAD:   $_branch" \
                "" \
                "This worktree was created for '$_bound'. Something switched it." \
                "Committing now would land your work on another agent's branch." \
                "" \
                "Remedy" \
                "  1. Go back:              git checkout $_bound" \
                "  2. New topic, new tree:  okuro wt add <topic>" \
                "  3. Intentional? Bypass:  OKURO_ALLOW_BRANCH_DRIFT=1 git commit ..."
        fi
    fi
fi

# --- SCOPE: denylisted paths may never be NEWLY added -----------------
# Only --diff-filter=A (additions). Already-tracked files were a
# deliberate past decision; this guard exists to stop `git add -A` from
# sweeping in environments, caches, databases and secrets.
_added="$(git diff --cached --diff-filter=A --name-only 2>/dev/null)"
if [ -n "$_added" ] && [ -z "$OKURO_ALLOW_SCOPE" ]; then
    _bad="$(printf '%s\n' "$_added" | grep -E \
        -e '(^|/)\.venv/' \
        -e '(^|/)venv/' \
        -e '(^|/)node_modules/' \
        -e '(^|/)__pycache__/' \
        -e '\.pyc$' \
        -e '(^|/)\.okuro/' \
        -e '(^|/)\.deploy-backups/' \
        -e '(^|/)deliverables/' \
        -e '\.(db|sqlite|sqlite3)$' \
        -e '(^|/)\.env$' \
        -e '(^|/)\.env\.' \
        -e '\.(pem|p12|pfx)$' \
        -e '(^|/)id_(rsa|ed25519|ecdsa)$' \
        || true)"
    if [ -n "$_bad" ]; then
        _lines=""
        while IFS= read -r _f; do
            [ -n "$_f" ] && _lines="$_lines
  - $_f"
        done <<< "$_bad"
        _die "SCOPE — commit adds denylisted paths" \
            "These files must not enter git history:$_lines" \
            "" \
            "Most often this is \`git add -A\` / \`git add .\` sweeping in an" \
            "environment, cache, database or secret." \
            "" \
            "Remedy" \
            "  1. Unstage them:        git restore --staged <path>" \
            "  2. Stage explicitly:    git add <specific files>" \
            "  3. Deliberate? Bypass:  OKURO_ALLOW_SCOPE=1 git commit ..."
    fi
fi

# --- SCOPE (base/brand boundary): the repo root is an ALLOWLIST -------
# Same invariant, same bypass: paths that must never enter history. The
# denylist above names environments and secrets; this names everything at
# the repo root that is not okuro the product. See PRODUCT_ROOT_ENTRIES in
# cmd_git_guards.py for why this is an allowlist and why it is root-anchored.
#
# Additions only, like the rest of SCOPE — an already-tracked file was a
# past decision and is not this guard's business. `git add -f` does NOT
# escape it: forcing past .gitignore still produces a staged addition.
#
# ONLY IN THE okuro REPO. `setup-git-guards --all-repos` installs these hooks
# into every repo the brain knows about (~18 paths share one hooks dir under
# one workspace repo alone), and "these root entries are the product" is a statement
# about okuro that is false everywhere else — applied blindly it would refuse
# every commit in every other repo. The denylist above is universal because
# a .env is a .env anywhere; an allowlist is not.
if [ -n "$_added" ] && [ -z "$OKURO_ALLOW_SCOPE" ] \
   && [ -f "$_root/src/okuro/cli/cmd_git_guards.py" ]; then
    _private=""
    while IFS= read -r _f; do
        [ -z "$_f" ] && continue
        case "${_f%%/*}" in
            __PRODUCT_ROOTS__) ;;
            *) _private="$_private
  - $_f" ;;
        esac
    done <<< "$_added"
    if [ -n "$_private" ]; then
        _die "SCOPE — commit adds paths that are not part of okuro" \
            "These live at the repo root but are not okuro the product:$_private" \
            "" \
            "okuro ships the base design system and okuro-ds. Your brands," \
            "stacks, deliverables and scratch work are YOUR data — they belong" \
            "in your instance, not in a repo that is PUBLIC on GitHub." \
            "" \
            "Remedy" \
            "  1. Unstage it:          git restore --staged <path>" \
            "  2. Keep it out of sight: add it to .gitignore" \
            "  3. This IS product? Add it to PRODUCT_ROOT_ENTRIES in" \
            "                          src/okuro/cli/cmd_git_guards.py," \
            "                          then: okuro setup-git-guards" \
            "  4. One-off? Bypass:     OKURO_ALLOW_SCOPE=1 git commit ..."
    fi
fi

# --- CONTENT: real brand/person tokens may not enter okuro's tree ------
# okuro-only, like the root allowlist. Paths say WHERE a commit writes;
# this reads WHAT it says: the ADDED lines of the staged diff, scanned
# against the user's own token list (real brands, customers, people).
# The list is ITSELF user data — it lives in ~/.okuro/guard/, never in
# this repo (a committed denylist would leak the names it protects; that
# exact mistake shipped once as a constant in design_systems/importer.py).
# A machine with no token files passes: a fresh install has no real data
# to protect. Details, anchoring and exclusions: okuro/cli/guard_content.py.
if [ -z "$OKURO_ALLOW_CONTENT" ] \
   && [ -f "$_root/src/okuro/cli/guard_content.py" ]; then
    _py="$_root/.venv/bin/python"
    [ -x "$_py" ] || _py="$(command -v python3 || command -v python || true)"
    if [ -n "$_py" ]; then
        _content_out="$(cd "$_root" && PYTHONPATH="$_root/src" "$_py" -m okuro.cli.guard_content scan-staged 2>&1)"
        if [ $? -ne 0 ]; then
            _die "CONTENT — commit adds real brand/person data" \
                "Staged additions carry tokens from ~/.okuro/guard/tokens.txt:" \
                "$_content_out" \
                "" \
                "Real brands, customers and people are YOUR data. Code in this" \
                "repo may only reference demo entities (northwind, meridian)." \
                "" \
                "Remedy" \
                "  1. Replace the token with a demo entity, or move the data" \
                "     to ~/.okuro / the DB — then restage." \
                "  2. False positive? Remove the token from ~/.okuro/guard/" \
                "     tokens.txt (or add # comment why it stays)." \
                "  3. Deliberate? Bypass:  OKURO_ALLOW_CONTENT=1 git commit ..."
        fi
    fi
fi

# --- SECRET: a credential may not enter the tree ----------------------
# NOT okuro-only: a leaked key is a leaked key in any repo this guard is
# installed in, and unlike CONTENT it needs no user token list to work.
# SCOPE already denies a `.env` FILE by path; a path denylist cannot see
# `API_KEY = "sk-..."` inside a .py, which is the shape that actually
# gets written. Precision over recall on purpose -- vendor-prefixed keys,
# PEM headers, and secret-NAMED variables holding a long literal, with
# placeholders and keyring/env reads excluded. Details in
# okuro/cli/guard_content.py.
if [ -z "$OKURO_ALLOW_SECRET" ] \
   && [ -f "$_root/src/okuro/cli/guard_content.py" ]; then
    _py="$_root/.venv/bin/python"
    [ -x "$_py" ] || _py="$(command -v python3 || command -v python || true)"
    if [ -n "$_py" ]; then
        _secret_out="$(cd "$_root" && PYTHONPATH="$_root/src" "$_py" -m okuro.cli.guard_content scan-secrets 2>&1)"
        if [ $? -ne 0 ]; then
            _die "SECRET — commit adds a credential" \
                "Staged additions carry what looks like a live secret:" \
                "$_secret_out" \
                "" \
                "Secrets belong in the keyring, never in the tree. A commit is" \
                "durable and a push is public: once it lands, rotating the key" \
                "is the only real remedy." \
                "" \
                "Remedy" \
                "  1. keyring_set(name, value) once, then read it at runtime" \
                "     with keyring_get(name) — then restage." \
                "  2. Placeholder or fixture? Make it obviously not real:" \
                "     <YOUR_KEY>, sk-example..., or shorter than 16 chars."
        fi
    fi
fi

# --- ADOPT: never silently commit agent-authored work you did not write -
# An untracked file carries NO provenance. Committing it converts "nobody
# has vouched for this" into "this repo ships it", permanently, under the
# committer's name. Measured 2026-07-28: a session found an orphaned
# module on disk -- untracked, zero callers, written by an earlier run it
# knew nothing about -- read it, tested it, and committed it to main.
#
# TWO conditions, because either alone is useless:
#
#   1. the file carries an AGENT_HEADER. Necessary but nowhere near
#      sufficient: okuro's own convention puts that header on EVERY new
#      source file an agent writes, so this condition alone would refuse
#      all legitimate work.
#   2. its BIRTH time predates the CURRENT HEAD commit. This is the
#      orphan signature: the file already existed while you committed
#      something else, so it is not work you just produced. A file you
#      created in this session is newer than your last commit.
#
# BIRTH TIME, NOT MTIME -- learned by watching the first cut of this guard
# stay silent on the exact case that motivated it. The adopting session had
# run mutation tests, which restore the file with `cp $BAK $SRC`. That
# refreshes mtime while creating nothing, so the file read as newer than
# HEAD and sailed through. Birth time survives it: `cp` truncates and
# rewrites the existing inode rather than replacing it, so %W still records
# when the file first appeared. Verified on this filesystem.
#
# Any content-touching tool defeats mtime the same way -- a formatter, a
# sed -i, a checkout. Only creation is the thing worth asking about.
#
# Fails OPEN by construction: no HEAD (initial commit), and any filesystem
# that does not record birth time (%W is 0 or "-") skips the check rather
# than guessing from mtime, which is the signal already known to lie.
if [ -n "$_added" ] && [ -z "$OKURO_ALLOW_ADOPT" ]; then
    _head_ts="$(git log -1 --format=%ct 2>/dev/null || echo 0)"
    if [ "$_head_ts" -gt 0 ] 2>/dev/null; then
        _orphans=""
        while IFS= read -r _f; do
            [ -z "$_f" ] && continue
            [ -f "$_f" ] || continue
            grep -q 'AGENT_HEADER' "$_f" 2>/dev/null || continue
            _mt="$(stat -c %W "$_f" 2>/dev/null || echo 0)"
            case "$_mt" in ''|*[!0-9]*) _mt=0 ;; esac
            [ "$_mt" -gt 0 ] 2>/dev/null || continue
            if [ "$_mt" -lt "$_head_ts" ]; then
                _orphans="$_orphans
  - $_f"
            fi
        done <<< "$_added"
        if [ -n "$_orphans" ]; then
            _die "ADOPT — commit adopts agent-authored work you did not write" \
                "These files carry an okuro AGENT_HEADER and were CREATED before" \
                "your own HEAD commit — they already existed while you committed" \
                "something else, so they are not work you produced here:$_orphans" \
                "" \
                "An untracked file has no author, no review and no approval." \
                "Committing it puts this repo's name on code nobody vouched for." \
                "" \
                "Remedy" \
                "  1. Find who made it:  trace_search(query='<filename>')" \
                "  2. Unstage it:        git restore --staged <path>" \
                "  3. Adopting it on purpose? Say so in the commit message, then:" \
                "                        OKURO_ALLOW_ADOPT=1 git commit ..."
        fi
    fi
fi

exit 0
""".replace("__CHAIN__", _CHAIN).replace("__PRELUDE__", _PRELUDE).replace(
    "__PRODUCT_ROOTS__", _PRODUCT_ROOTS_CASE
)


PRE_PUSH_HOOK = r"""#!/usr/bin/env bash
# okuro history-protection guard — installed by `okuro setup-git-guards`.
# Enforces PROTECT (main may never be force-pushed or deleted) and PUBLISH
# (a push may not make non-product paths public).
#
# stdin protocol (git pre-push): <local-ref> <local-sha> <remote-ref> <remote-sha>
_zero="0000000000000000000000000000000000000000"

# Read stdin ONCE — a chained hook (git-lfs) needs the same ref list we do,
# and stdin can only be consumed once.
_stdin="$(cat)"

if [ -x "$0.okuro-chained" ]; then
    printf '%s\n' "$_stdin" | "$0.okuro-chained" "$@" || exit $?
fi

# --- PUBLISH: only okuro the product may become public ----------------
# The commit-time SCOPE guard refuses an addition; this refuses the event
# that actually makes it public, and it is the only guard that can. Two
# things are knowable here and nowhere else: the FULL RANGE of commits a
# push would hand over, and the DESTINATION it hands them to.
#
# Fires on every remote by default. ONE exemption, decided 2026-08-09:
# a destination recorded in ~/.okuro/guard/private-remotes.txt skips the
# range path rule (b) — under release-by-export EVERY dev push carries
# non-product history, so without this the guard fires on every push and
# OKURO_ALLOW_PUBLISH=1 becomes a habit that also silences the release
# repo's protection. The --all-shape rule (a) holds everywhere: bulk
# publication is wrong even to a private remote. Fail-closed: no file,
# or a URL not in it, and nothing is exempt. Recording a remote there is
# a deliberate user act — the same trust model as guard/tokens-extra.txt.
_remote_name="$1"
_remote_url="$2"

_dest_private=0
_privfile="${OKURO_HOME:-$HOME/.okuro}/guard/private-remotes.txt"
if [ -f "$_privfile" ]; then
    _url_norm="${_remote_url%.git}"
    while IFS= read -r _pline; do
        _pline="${_pline%%#*}"
        _pline="$(printf '%s' "$_pline" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        [ -z "$_pline" ] && continue
        if [ "${_pline%.git}" = "$_url_norm" ]; then _dest_private=1; break; fi
    done < "$_privfile"
fi

if [ -z "$OKURO_ALLOW_PUBLISH" ]; then
    # (a) `--all` / `--mirror` shape. A deliberate push carries one ref,
    # occasionally two. Measured 2026-08-05: this repo holds 134 local
    # branches against 5 on the remote, so one absent-minded `--all`
    # publishes 129 branches nobody reviewed.
    _nrefs="$(printf '%s\n' "$_stdin" | grep -c '[^[:space:]]' || true)"
    if [ "${_nrefs:-0}" -gt 5 ] 2>/dev/null; then
        echo >&2 ""
        echo >&2 "════════════════════════════════════════════════════════════════════"
        echo >&2 "  ✗ okuro-git-guard: PUBLISH — refusing a $_nrefs-ref push"
        echo >&2 "════════════════════════════════════════════════════════════════════"
        echo >&2 ""
        echo >&2 "  Remote: $_remote_name ($_remote_url)"
        echo >&2 ""
        echo >&2 "  This is the shape of \`git push --all\` or \`--mirror\`. Branch"
        echo >&2 "  names and every commit on them become public in one step."
        echo >&2 ""
        echo >&2 "  Remedy"
        echo >&2 "    1. Push one branch:     git push $_remote_name <branch>"
        echo >&2 "    2. Deliberate? Bypass:  OKURO_ALLOW_PUBLISH=1 git push ..."
        echo >&2 ""
        exit 1
    fi

    # (b) Every path this push would newly publish. For a branch the remote
    # already has, that is the range between the two tips; for a new branch,
    # every commit not already reachable from a remote — NOT the whole tree,
    # which would re-flag history the remote has had for months.
    #
    # Measured 2026-08-05: 0.47s across the 2551 commits main is ahead by.
    #
    # okuro-only, for the reason given at the SCOPE root check in the
    # pre-commit hook: "this is the product" does not translate to another
    # repo, and --all-repos installs this hook into every one of them.
    # Skipped for a recorded private remote (see _dest_private above).
    _pubroot="$(git rev-parse --show-toplevel 2>/dev/null)"
    if [ "$_dest_private" = "0" ] && [ -n "$_pubroot" ] && [ -f "$_pubroot/src/okuro/cli/cmd_git_guards.py" ]; then
        _added_pub=""
        while read -r _lref _lsha _rref _rsha; do
            [ -z "$_rref" ] && continue
            [ "$_lsha" = "$_zero" ] && continue   # deletion publishes nothing
            if [ "$_rsha" = "$_zero" ]; then
                _range="$_lsha --not --remotes"
            else
                _range="$_rsha..$_lsha"
            fi
            # $_range is deliberately unquoted — it carries multiple git args.
            _added_pub="$_added_pub
$(git log --diff-filter=A --name-only --pretty=format: $_range 2>/dev/null)"
        done <<< "$_stdin"

        _leak=""
        while IFS= read -r _f; do
            [ -z "$_f" ] && continue
            case "${_f%%/*}" in
                __PRODUCT_ROOTS__) ;;
                *) _leak="$_leak
  - $_f" ;;
            esac
        done <<< "$(printf '%s\n' "$_added_pub" | sort -u)"

        if [ -n "$_leak" ]; then
            echo >&2 ""
            echo >&2 "════════════════════════════════════════════════════════════════════"
            echo >&2 "  ✗ okuro-git-guard: PUBLISH — push would make non-product paths public"
            echo >&2 "════════════════════════════════════════════════════════════════════"
            echo >&2 ""
            echo >&2 "  Remote: $_remote_name ($_remote_url)"
            echo >&2 "$_leak"
            echo >&2 ""
            echo >&2 "  okuro ships the base design system and okuro-ds. Brands, stacks,"
            echo >&2 "  deliverables and scratch work are the USER's data. Once pushed,"
            echo >&2 "  a public git history cannot be taken back — only rewritten, and"
            echo >&2 "  only if nobody has cloned it."
            echo >&2 ""
            echo >&2 "  Remedy"
            echo >&2 "    1. Untrack it, keep the file:  git rm --cached -r <path>"
            echo >&2 "    2. Then ignore it:             echo '<path>/' >> .gitignore"
            echo >&2 "    3. This IS product? Add it to PRODUCT_ROOT_ENTRIES in"
            echo >&2 "                                   src/okuro/cli/cmd_git_guards.py,"
            echo >&2 "                                   then: okuro setup-git-guards"
            echo >&2 "    4. Destination is a PRIVATE dev remote? Record it once:"
            echo >&2 "                                   echo '<url>' >> ~/.okuro/guard/private-remotes.txt"
            echo >&2 "    5. Deliberate? Bypass:         OKURO_ALLOW_PUBLISH=1 git push ..."
            echo >&2 ""
            exit 1
        fi
    fi
fi

# Here-string, not a pipe: a `while` on the right of a pipe runs in a
# subshell where `exit 1` would not abort the push.
while read -r _lref _lsha _rref _rsha; do
    [ -z "$_rref" ] && continue
    case "$_rref" in
        refs/heads/main) ;;
        *) continue ;;
    esac

    # Deleting remote main.
    if [ "$_lsha" = "$_zero" ]; then
        if [ -z "$OKURO_ALLOW_MAIN_REWRITE" ]; then
            echo >&2 ""
            echo >&2 "  ✗ okuro-git-guard: PROTECT — refusing to DELETE remote main."
            echo >&2 "    Bypass: OKURO_ALLOW_MAIN_REWRITE=1 git push ..."
            echo >&2 ""
            exit 1
        fi
        continue
    fi

    # New branch on the remote — nothing to rewrite.
    [ "$_rsha" = "$_zero" ] && continue

    # Non-fast-forward: the remote tip is not an ancestor of what we push.
    if ! git merge-base --is-ancestor "$_rsha" "$_lsha" 2>/dev/null; then
        if [ -z "$OKURO_ALLOW_MAIN_REWRITE" ]; then
            _lost="$(git rev-list --count "$_lsha".."$_rsha" 2>/dev/null || echo '?')"
            echo >&2 ""
            echo >&2 "════════════════════════════════════════════════════════════════════"
            echo >&2 "  ✗ okuro-git-guard: PROTECT — non-fast-forward push to main"
            echo >&2 "════════════════════════════════════════════════════════════════════"
            echo >&2 ""
            echo >&2 "  Remote main has $_lost commit(s) your push would discard."
            echo >&2 ""
            echo >&2 "  Remedy"
            echo >&2 "    1. Integrate first:     git pull --rebase origin main"
            echo >&2 "    2. Deliberate? Bypass:  OKURO_ALLOW_MAIN_REWRITE=1 git push ..."
            echo >&2 ""
            exit 1
        fi
    fi
done <<< "$_stdin"

exit 0
""".replace("__PRODUCT_ROOTS__", _PRODUCT_ROOTS_CASE)


POST_CHECKOUT_HOOK = r"""#!/usr/bin/env bash
# okuro concurrent-session guard — installed by `okuro setup-git-guards`.
# ADVISORY ONLY. Warns early about branch state that the pre-commit guard
# will later refuse, so the feedback arrives before the work, not after.
__CHAIN__
# Args per git docs: $1=prev_head $2=new_head $3=branch_flag
# branch_flag is 1 for `git checkout <branch>`, 0 for file checkout.
# NOTE: the chain above runs FIRST and unconditionally — git-lfs
# post-checkout must fire on file checkouts too, which this guard skips.
[ "$3" != "1" ] && exit 0
__PRELUDE__
[ -z "$_branch" ] && exit 0      # detached HEAD

# --- main worktree: warn on any switch away from main -----------------
if [ "$_is_main_tree" = "1" ]; then
    [ "$_branch" = "main" ] && exit 0
    [ -n "$OKURO_ALLOW_MAIN_SWITCH" ] && exit 0
    cat >&2 <<EOF

════════════════════════════════════════════════════════════════════
  ⚠  BRANCH SWITCH IN MAIN WORKTREE — $_branch
════════════════════════════════════════════════════════════════════

Repo:  $_root
To:    $_branch

Any concurrent Claude Code / Codex / Gemini session that spawns an MCP
stdio subprocess for this repo will now load code FROM THIS BRANCH, not
from main. Services pinned to the main tree's venv may pick this up on
next restart.

The pre-commit guard (PIN) will REFUSE commits while this tree is off
main.

Remedy
  1. Switch back:        git checkout main
  2. For branch work:    okuro wt add <topic>
  3. Intentional? Set:   export OKURO_ALLOW_MAIN_SWITCH=1

See principle ORCH-PARALLEL-TREE (bootstrap).
════════════════════════════════════════════════════════════════════

EOF
    exit 0
fi

# --- sibling worktree: warn when HEAD drifts off the bound branch -----
if [ -f "$_root/.okuro-worktree" ]; then
    _bound="$(sed -n 's/^branch=//p' "$_root/.okuro-worktree" | head -1)"
    if [ -n "$_bound" ] && [ "$_bound" != "$_branch" ]; then
        [ -n "$OKURO_ALLOW_BRANCH_DRIFT" ] && exit 0
        cat >&2 <<EOF

════════════════════════════════════════════════════════════════════
  ⚠  WORKTREE DRIFTED OFF ITS BOUND BRANCH
════════════════════════════════════════════════════════════════════

Tree:   $_root
Bound:  $_bound
HEAD:   $_branch

This worktree was created for '$_bound'. The pre-commit guard (BIND)
will REFUSE commits until HEAD returns to it.

Remedy
  1. Go back:             git checkout $_bound
  2. New topic, new tree: okuro wt add <topic>
════════════════════════════════════════════════════════════════════

EOF
    fi
fi

exit 0
""".replace("__CHAIN__", _CHAIN).replace("__PRELUDE__", _PRELUDE)


SHELL_WRAPPER_SNIPPET = r"""# Optional. Paste into ~/.bashrc to turn the post-checkout WARNING into a
# pre-checkout BLOCK. The pre-commit hook already refuses the commit; this
# just moves the failure earlier, before work happens on the wrong branch.
okuro_git_guard() {
    local gitdir commondir branch target
    if [ "$1" != "checkout" ] && [ "$1" != "switch" ]; then
        command git "$@"; return $?
    fi
    if [ -n "$OKURO_ALLOW_MAIN_SWITCH" ]; then
        command git "$@"; return $?
    fi
    gitdir="$(command git rev-parse --git-dir 2>/dev/null)" || { command git "$@"; return $?; }
    commondir="$(command git rev-parse --git-common-dir 2>/dev/null)"
    if [ "$(cd "$gitdir" && pwd -P)" != "$(cd "$commondir" && pwd -P)" ]; then
        command git "$@"; return $?   # inside a sibling worktree, no guard
    fi
    branch="$(command git symbolic-ref --short HEAD 2>/dev/null)"
    if [ "$branch" != "main" ]; then command git "$@"; return $?; fi
    target="${*: -1}"
    if [ "$target" = "main" ] || [ -z "$target" ]; then command git "$@"; return $?; fi
    echo >&2 ""
    echo >&2 "  ✗ okuro-git-guard: refusing to switch from main to '$target' in the main tree."
    echo >&2 "    Use: okuro wt add <topic>"
    echo >&2 "    Or:  OKURO_ALLOW_MAIN_SWITCH=1 git $*"
    echo >&2 ""
    return 1
}
git() { okuro_git_guard "$@"; }
"""


# name -> (script, identifying marker used for idempotent reinstall)
HOOKS = {
    "pre-commit": (PRE_COMMIT_HOOK, "okuro branch-integrity guard"),
    "pre-push": (PRE_PUSH_HOOK, "okuro history-protection guard"),
    "post-checkout": (POST_CHECKOUT_HOOK, "okuro concurrent-session guard"),
}


def _is_git_repo(path: Path) -> bool:
    """True if ``path`` is inside a git repository."""
    try:
        subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            check=True, capture_output=True, text=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _hooks_dir(repo: Path) -> Path:
    """Resolve the hooks directory honoring ``core.hooksPath`` if set.

    Resolves to the *common* dir, so a single install covers the main
    worktree and every sibling created by ``git worktree add``.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "core.hooksPath"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if out:
            return (repo / out).resolve() if not Path(out).is_absolute() else Path(out)
    except subprocess.CalledProcessError:
        pass
    common_dir = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = (repo / common_path).resolve()
    return common_path / "hooks"


def _worktree_roots(repo: Path) -> list[tuple[Path, str]]:
    """Return ``(root, branch)`` for every worktree of ``repo``.

    Detached-HEAD worktrees are skipped — there is no branch to bind to.
    """
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    results: list[tuple[Path, str]] = []
    current: Path | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):])
        elif line.startswith("branch ") and current is not None:
            ref = line[len("branch "):]
            results.append((current, ref.removeprefix("refs/heads/")))
            current = None
    return results


def _backfill_bindings(repo: Path) -> tuple[int, int]:
    """Write ``.okuro-worktree`` into every sibling worktree missing one.

    The main worktree is skipped: PIN already pins it to ``main``, so a
    binding file there would be redundant. Returns ``(written, skipped)``.
    """
    written = skipped = 0
    main_root = Path(
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    ).parent.resolve()

    for root, branch in _worktree_roots(repo):
        if root.resolve() == main_root:
            continue
        marker = root / ".okuro-worktree"
        if marker.exists():
            skipped += 1
            continue
        marker.write_text(
            "# okuro worktree binding — written by `okuro setup-git-guards`.\n"
            "# The pre-commit BIND guard refuses commits when HEAD != branch.\n"
            f"repo={repo.name}\n"
            f"branch={branch}\n"
        )
        written += 1
    return written, skipped


def _registered_repos() -> list[Path]:
    """Every repo the okuro brain knows about: managed clones + projects."""
    from ..db import get_db

    db = get_db()
    paths: list[Path] = []
    seen: set[str] = set()
    for sql in (
        "SELECT path FROM managed_repos WHERE path IS NOT NULL AND path != ''",
        "SELECT path FROM projects WHERE active = 1 AND path IS NOT NULL AND path != ''",
    ):
        try:
            rows = db.fetchall(sql)
        except Exception:
            continue
        for row in rows:
            p = str(row["path"])
            # Many project rows carry a slug-ish placeholder ('unknown/foo')
            # rather than a real location. Only absolute paths name a repo on
            # this machine; a relative one would otherwise be resolved against
            # the caller's cwd and could match an unrelated repo.
            if not p.startswith("/") or p in seen:
                continue
            seen.add(p)
            paths.append(Path(p))
    return paths


def _write_hook(
    hooks_dir: Path, name: str, script: str, marker: str, force: bool
) -> str:
    """Write one hook, chaining any foreign one it displaces.

    A pre-existing non-okuro hook (git-lfs, husky, ...) is MOVED to
    ``<name>.okuro-chained`` and invoked first by the okuro hook, which
    forwards its exit code. Overwriting outright would silently break
    git-lfs in every repo that uses it, so ``--force`` does not do that
    either — it only forces a re-chain of an already-chained hook.

    Returns 'installed', 'installed+chained', 'updated' or 'unchanged'.
    """
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / name
    chained = hooks_dir / f"{name}.okuro-chained"
    status = "installed"

    if target.exists():
        existing = target.read_text(errors="replace")
        if marker in existing:
            # Ours already — a re-run just refreshes the script.
            status = "unchanged" if existing == script else "updated"
            if status == "unchanged":
                return status
        else:
            # Foreign hook. Preserve it rather than destroy it.
            if chained.exists() and not force:
                raise click.ClickException(
                    f"{chained} already exists; refusing to overwrite a "
                    "previously chained hook. Pass --force to re-chain."
                )
            target.replace(chained)
            chained.chmod(
                chained.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
            status = "installed+chained"

    target.write_text(script)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return status


@click.command("setup-git-guards")
@click.option(
    "--repo", "repo_arg", default=None,
    help="Target repo path. Defaults to the current directory.",
)
@click.option(
    "--all-repos", is_flag=True,
    help="Install into every repo registered in the okuro brain.",
)
@click.option(
    "--force", is_flag=True,
    help="Re-chain a hook that was already chained (never destroys a foreign hook).",
)
@click.option(
    "--no-backfill", is_flag=True,
    help="Skip writing .okuro-worktree bindings into existing worktrees.",
)
@click.option(
    "--print-shell-wrapper", is_flag=True,
    help="Print the optional bash function that blocks checkout earlier.",
)
def setup_git_guards(repo_arg, all_repos, force, no_backfill, print_shell_wrapper):
    """Install provider-agnostic branch-integrity hooks in a git repo.

    \b
    PIN      main worktree may only commit to main          (pre-commit)
    BIND     a worktree may only commit to its bound branch (pre-commit)
    SECRET   no credential in a staged addition
    SCOPE    denylisted paths never added; repo root is an
             allowlist of okuro the product                 (pre-commit)
    ADOPT    no silent commit of agent work you did not write (pre-commit)
    PROTECT  no force-push or deletion of main              (pre-push)
    PUBLISH  a push may not make non-product paths public   (pre-push)

    Enforcement is in git, so it applies identically to Claude Code,
    Codex, Gemini, Cursor, cron and human shells — anything that shells
    out to ``git``. Hooks install into the git *common* dir, so one run
    covers the main worktree and every sibling.
    """
    if print_shell_wrapper:
        heading("Shell wrapper (blocks checkout in the main tree)")
        info("Paste into ~/.bashrc (requires a new shell to take effect):")
        console.print()
        console.print(SHELL_WRAPPER_SNIPPET)
        return

    if all_repos:
        try:
            repos = _registered_repos()
        except Exception as exc:
            fail(f"Could not read registered repos: {exc}")
            raise SystemExit(1)
        if not repos:
            fail("No repos registered in the okuro brain.")
            raise SystemExit(1)
    else:
        repos = [Path(repo_arg or ".").resolve()]

    failures = 0
    for repo in repos:
        if not _is_git_repo(repo):
            warn(f"Not a git repo, skipping: {repo}")
            continue

        heading(f"Installing git guards in {repo}")

        try:
            hooks_dir = _hooks_dir(repo)
        except subprocess.CalledProcessError as exc:
            fail(f"Could not resolve hooks dir: {exc}")
            failures += 1
            continue

        try:
            for name, (script, marker) in HOOKS.items():
                status = _write_hook(hooks_dir, name, script, marker, force=force)
                if status == "unchanged":
                    info(f"{name}: already current (no-op)")
                else:
                    ok(f"{name}: {status} -> {hooks_dir / name}")
        except click.ClickException as exc:
            fail(str(exc))
            failures += 1
            continue
        except Exception as exc:
            fail(f"Could not write hooks: {exc}")
            failures += 1
            continue

        if not no_backfill:
            try:
                written, skipped = _backfill_bindings(repo)
                if written:
                    ok(f".okuro-worktree: wrote {written} binding(s)")
                if skipped:
                    info(f".okuro-worktree: {skipped} already bound")
            except Exception as exc:
                warn(f"Backfill failed (hooks still active): {exc}")

    console.print()
    info("Bypass env vars (per invariant, use sparingly):")
    info("  OKURO_ALLOW_MAIN_COMMIT   PIN     — commit off main in the main tree")
    info("  OKURO_ALLOW_BRANCH_DRIFT  BIND    — commit off a worktree's bound branch")
    info("  OKURO_ALLOW_SCOPE         SCOPE   — add a denylisted or non-product path")
    info("  OKURO_ALLOW_CONTENT       CONTENT — commit a real brand/person token")
    info("  OKURO_ALLOW_SECRET        SECRET  — commit a credential-shaped string")
    info("  OKURO_ALLOW_ADOPT         ADOPT   — commit agent work you did not write")
    info("  OKURO_ALLOW_MAIN_REWRITE  PROTECT — force-push or delete main")
    info("  OKURO_ALLOW_PUBLISH       PUBLISH — publish non-product paths / push --all")
    info("  OKURO_ALLOW_MAIN_SWITCH   (advisory post-checkout warning only)")

    if failures:
        raise SystemExit(1)
