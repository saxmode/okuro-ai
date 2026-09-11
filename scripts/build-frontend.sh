#!/usr/bin/env bash
# Build the okuro web SPA into src/okuro/web/dist.
#
# SINGLE SOURCE OF TRUTH for the frontend build — called by BOTH install.sh
# (fresh install) and scripts/update.sh (incremental update). Before this
# existed the build lived only in update.sh, so a fresh `git clone &&
# ./install.sh` produced no dist and the window rendered blank (installability
# audit 2026-06-04, P0). Keep it that way: any path that needs a built SPA
# calls THIS script.
#
# Usage: build-frontend.sh [frontend_dir]
#   frontend_dir defaults to <repo>/src/okuro/web/frontend
set -euo pipefail

# This script is a non-interactive build step — nothing below may ever ask a
# question. Two independent guards, because a single prompt here is unusually
# expensive to diagnose (see below).
#
# 1. corepack's pnpm shim defaults COREPACK_ENABLE_DOWNLOAD_PROMPT to 1. On a
#    cold cache — i.e. ANY fresh machine — resolving package.json's pinned
#    `packageManager: pnpm@x` needs a download, so corepack writes
#    `? Do you want to continue? [Y/n]` to stderr and blocks on stdin.
# 2. `exec < /dev/null` closes stdin outright, so corepack's `stdin.isTTY`
#    check is false and any FUTURE tool that tries to prompt gets EOF and
#    fails fast instead of hanging.
#
# Why this hung silently rather than showing the prompt: install.sh pipes us
# through `sed 's/^/    /'`, and sed's own stdout is a pipe to the transcript
# tee — not a tty — so sed block-buffers at 4KB. The prompt is ~120 bytes with
# no trailing newline, so it never left sed's buffer. The installer just went
# mute and waited forever at [5/11]. Reproduced on two fresh Macs, 2026-07-15.
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
exec < /dev/null

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FE="${1:-$REPO_ROOT/src/okuro/web/frontend}"

if [ ! -f "$FE/package.json" ]; then
    echo "build-frontend: no package.json at $FE" >&2
    exit 1
fi

# pnpm acquisition cascade (mirrors update.sh's, intentionally layered to
# cover every common Node setup):
#   1. Volta toolchain   2. pnpm already on PATH
#   3. corepack enable    4. npm install -g pnpm
# Echoes the chosen mode on stdout; all installer chatter goes to stderr so
# the captured mode stays clean.
ensure_pnpm() {
    if command -v volta >/dev/null 2>&1; then
        if ! volta list pnpm 2>/dev/null | grep -q '^[[:space:]]*pnpm@'; then
            echo "[build-frontend] volta detected — installing pnpm into toolchain" >&2
            volta install pnpm >&2 || return 1
        fi
        echo "volta"; return 0
    fi
    if command -v pnpm >/dev/null 2>&1; then echo "direct"; return 0; fi
    if command -v corepack >/dev/null 2>&1; then
        echo "[build-frontend] enabling pnpm via corepack" >&2
        corepack enable pnpm >/dev/null 2>&1 || true
        if command -v pnpm >/dev/null 2>&1; then echo "direct"; return 0; fi
    fi
    if command -v npm >/dev/null 2>&1; then
        echo "[build-frontend] installing pnpm via npm" >&2
        npm install -g pnpm >&2 || return 1
        echo "direct"; return 0
    fi
    echo "[build-frontend] no Node tooling on PATH — install Node first." >&2
    return 1
}

# `build` = the SPA (→ web/dist); `build:export` = the Prism single-file HTML
# export template (→ web/export-dist), read by /api/prism/{id}/export;
# `build:export-deck` = the deck2 single-file template (→ web/export-dist/deck),
# read by /api/prism/deck2/{id}/export. All three must exist post-install or the
# matching export endpoint 503s. ORDER MATTERS: build:export empties export-dist
# (emptyOutDir), so build:export-deck MUST run AFTER it — otherwise the deck/
# subdir is wiped and deck2 export 503s.
MODE="$(ensure_pnpm)"
case "$MODE" in
    volta)  ( cd "$FE" && volta run pnpm install --frozen-lockfile && volta run pnpm build && volta run pnpm build:export && volta run pnpm build:export-deck ) ;;
    direct) ( cd "$FE" && pnpm install --frozen-lockfile && pnpm build && pnpm build:export && pnpm build:export-deck ) ;;
    *) echo "build-frontend: unknown pnpm mode '$MODE'" >&2; exit 1 ;;
esac
