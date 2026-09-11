#!/usr/bin/env bash
# okuro bootstrap — zero-to-running install AND update, with no pre-existing
# git checkout. The Linux/macOS entry point, delivered over HTTPS:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/saxmode/okuro-ai/main/bootstrap.sh)"
#
# (`bash -c "$(curl …)"` rather than `curl | bash`: the script keeps a real
# stdin, so sudo and Homebrew can prompt you.)
#
# It ensures the whole toolchain (macOS: Xcode CLT → Homebrew → python@3.12 →
# node; Linux: git/python/node via the system package manager), then clones
# okuro (first run) or updates it (subsequent runs) and hands off to
# install.sh / update.sh. Because it is fetched fresh every time, it is also
# the one place migration logic can reach a checkout that predates it.
#
# Idempotent + resumable: every step is skipped if already satisfied, so a
# re-run after a network drop or a half-finished Homebrew install continues
# where it left off.
#
# Usage:
#   ./bootstrap.sh            # install or update
#   ./bootstrap.sh --check    # report toolchain state, change NOTHING
#   ./bootstrap.sh --help
#
# Env overrides:
#   OKURO_REPO_URL      git URL (default: public okuro-ai)
#   OKURO_INSTALL_DIR   clone target (default: ~/okuro-ai)

set -uo pipefail   # NOT -e: errors are handled explicitly so re-runs resume

REPO_URL="${OKURO_REPO_URL:-https://github.com/saxmode/okuro-ai.git}"
INSTALL_DIR="${OKURO_INSTALL_DIR:-$HOME/okuro-ai}"
PY_FORMULA="python@3.12"
LOG="$HOME/.okuro-bootstrap.log"
CHECK=0

for a in "${@:-}"; do
    case "$a" in
        --check) CHECK=1 ;;
        -h|--help) if [ -f "$0" ]; then sed -n '2,28p' "$0"; else echo "okuro bootstrap — install or update okuro; --check reports the toolchain and changes nothing"; fi; exit 0 ;;
        "") ;;
        *) echo "unknown arg: $a" >&2; exit 1 ;;
    esac
done

log()  { printf '%s\n' "$*" | tee -a "$LOG"; }
step() { log ""; log "── $* ──"; }
die()  { log "ERROR: $*"; log "See $LOG — re-run this installer to resume."; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

ensure_brew_env() {
    [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
    [ -x /usr/local/bin/brew ]   && eval "$(/usr/local/bin/brew shellenv)"   2>/dev/null || true
}

# ── check mode — report only, no mutations ──────────────────────────────────
if [ "$CHECK" = "1" ]; then
    ensure_brew_env
    echo "okuro bootstrap — toolchain check ($(uname))"
    printf '  %-22s %s\n' "git"       "$(have git && git --version 2>/dev/null || echo MISSING)"
    printf '  %-22s %s\n' "python3"   "$(have python3 && python3 --version 2>/dev/null || echo MISSING)"
    printf '  %-22s %s\n' "node"      "$(have node && node --version 2>/dev/null || echo MISSING)"
    if [ "$(uname)" = "Darwin" ]; then
        printf '  %-22s %s\n' "xcode CLT" "$(xcode-select -p 2>/dev/null || echo MISSING)"
        printf '  %-22s %s\n' "homebrew"  "$(have brew && brew --version 2>/dev/null | head -1 || echo MISSING)"
    fi
    printf '  %-22s %s\n' "okuro checkout" "$([ -d "$INSTALL_DIR/.git" ] && echo "$INSTALL_DIR (update)" || echo "absent (fresh install)")"
    exit 0
fi

: > "$LOG" 2>/dev/null || true
step "okuro bootstrap starting ($(uname)) — log: $LOG"

# ── toolchain ───────────────────────────────────────────────────────────────
ensure_macos_toolchain() {
    if ! xcode-select -p >/dev/null 2>&1; then
        step "Installing Apple Command Line Tools — click Install in the Apple dialog"
        xcode-select --install 2>/dev/null || true
        log "Waiting for Command Line Tools to finish installing…"
        # Bounded: an unbounded `until` hangs forever after one printed line
        # if the dialog is dismissed or the download fails. Nudge every
        # minute, give up after 30 — the install itself is ~5-15 min.
        waited=0
        until xcode-select -p >/dev/null 2>&1; do
            sleep 5
            waited=$((waited + 5))
            if [ $((waited % 60)) -eq 0 ]; then
                log "  still waiting ($((waited / 60)) min) — if you closed the dialog, run: xcode-select --install"
            fi
            if [ "$waited" -ge 1800 ]; then
                die "Command Line Tools did not appear within 30 min — run 'xcode-select --install', finish the dialog, then re-run this installer"
            fi
        done
    fi
    ensure_brew_env
    if ! have brew; then
        step "Installing Homebrew (you may be prompted for your Mac password)"
        NONINTERACTIVE=1 /bin/bash -c \
            "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
            || die "Homebrew install failed"
        ensure_brew_env
    fi
    have brew || die "brew not on PATH after install — open a new terminal and re-run"
    if ! brew list "$PY_FORMULA" >/dev/null 2>&1; then
        step "Installing $PY_FORMULA"
        brew install "$PY_FORMULA" || die "python install failed"
    fi
    if ! have node; then
        step "Installing node"
        brew install node || die "node install failed"
    fi
}

ensure_linux_toolchain() {
    if have git && have python3 && have node; then return 0; fi
    step "Installing git / python / node via the system package manager"
    if have apt-get; then
        sudo apt-get update && sudo apt-get install -y git python3 python3-venv nodejs npm || die "apt install failed"
    elif have dnf; then
        sudo dnf install -y git python3 nodejs npm || die "dnf install failed"
    elif have pacman; then
        sudo pacman -S --noconfirm git python nodejs npm || die "pacman install failed"
    else
        die "no supported package manager — install git, python3, node manually"
    fi
}

case "$(uname)" in
    Darwin) ensure_macos_toolchain ;;
    Linux)  ensure_linux_toolchain ;;
    *) die "unsupported OS: $(uname)" ;;
esac

have git || die "git still missing after toolchain setup"


# ── full snapshot of the okuro home before anything moves ───────────────────
# The re-clone never touches ~/.okuro, but the migration that follows does —
# and on a checkout old enough to need a re-clone, the installed okuro's own
# `backup create` is the OLD one, covering whatever it covered back then
# (measured 2026-09-09: database and keyring, not config, tokens, corpora or
# deliveries). This script is the only fresh code on such a machine, so it
# takes the complete copy itself: every entry of the okuro home except
# regenerable caches and the backups, as a sibling directory. A failed or
# unaffordable snapshot ABORTS with nothing changed.
snapshot_okuro_home() {   # <dest>
    local src="${OKURO_HOME:-$HOME/.okuro}" dest="$1"
    [ -d "$src" ] || { log "  no $src yet — nothing to snapshot"; return 0; }
    local ex="backups .deploy-backups hf models comfyui cortex prism-cache cache webview voice-previews repos proof logs"
    local need kb e
    need="$(du -sk "$src" 2>/dev/null | cut -f1)"
    for e in $ex; do
        [ -e "$src/$e" ] && { kb="$(du -sk "$src/$e" 2>/dev/null | cut -f1)"; need=$((need - ${kb:-0})); }
    done
    local free; free="$(df -Pk "$(dirname "$dest")" | awk 'NR==2{print $4}')"
    [ "${free:-0}" -gt $((need + need / 10)) ] \
        || die "not enough disk for a full snapshot of $src (need ~$((need / 1024)) MB, $((${free:-0} / 1024)) MB free) — nothing was changed"
    mkdir -p "$dest" || die "cannot create $dest"
    local args=(); for e in $ex; do args+=("--exclude=./$e"); done
    if ! ( cd "$src" && tar -cf - "${args[@]}" . ) | ( cd "$dest" && tar -xpf - ); then
        die "snapshot to $dest failed — nothing was changed"
    fi
    log "  full copy of $src kept at $dest (everything but caches; delete it once the new install checks out)"
}

# ── clone or update, then hand off ──────────────────────────────────────────
# CLASS (identical to scripts/update.sh and bootstrap.ps1): a ff-only
# distribution channel whose upstream lineage can be legitimately REPLACED.
# okuro ships by clone + ff-only pull, so when the public repo's history is
# regenerated — a release lineage with its own root commit — no clone made
# under the old history can ever fast-forward again. The signal is the ABSENCE
# OF A MERGE BASE between HEAD and the fetched upstream (exit 1 from
# merge-base; 0 = shared history, 128 = bad ref, not ours to interpret). A
# plain divergence keeps failing exactly as before.
#
# This script is the one copy of that logic a stale checkout can receive: the
# checkout's own update.sh predates the swap by definition. Migration is safe
# because ~/.okuro holds ALL user state; the checkout is code plus build
# output install.sh regenerates.
if [ -d "$INSTALL_DIR/.git" ]; then
    step "Updating okuro at $INSTALL_DIR"
    cd "$INSTALL_DIR" || die "cannot enter $INSTALL_DIR"
    # pull is split into fetch + merge so the lineage check can sit between.
    git fetch origin || die "git fetch failed — check your network"
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
    [ -n "$branch" ] && [ "$branch" != "HEAD" ] || branch=main
    upstream="origin/$branch"
    swapped=0
    if git rev-parse --verify --quiet "${upstream}^{commit}" >/dev/null 2>&1; then
        rc=0
        git merge-base HEAD "$upstream" >/dev/null 2>&1 || rc=$?
        [ "$rc" = "1" ] && swapped=1
    fi

    if [ "$swapped" = "0" ]; then
        git merge --ff-only "$upstream" || die "git merge --ff-only failed — local changes? run: cd $INSTALL_DIR && git status"
        exec bash scripts/update.sh
    fi

    step "Upstream lineage replaced — migrating $INSTALL_DIR by re-clone"
    log "  $upstream shares no history with your checkout, so a fast-forward is"
    log "  impossible and always will be. Your data lives in ~/.okuro, not here —"
    log "  this directory is code plus build output install.sh rebuilds."

    ts="$(date +%Y%m%d-%H%M%S)"
    snapshot_okuro_home "${OKURO_HOME:-$HOME/.okuro}.pre-reclone-${ts}"

    # Then the installed okuro's own backup too — its database copy is
    # WAL-consistent, which a file copy of a live database is not. A failed
    # backup ABORTS with the checkout untouched. No okuro binary means
    # nothing installed to back up.
    okuro_bin=""
    for cand in "$INSTALL_DIR/.venv/bin/okuro" "$INSTALL_DIR/venv/bin/okuro"; do
        [ -x "$cand" ] && { okuro_bin="$cand"; break; }
    done
    if [ -n "$okuro_bin" ]; then
        log "  backing up current state (DB + keyring) before re-clone…"
        "$okuro_bin" backup create --label pre-reclone \
            || die "backup FAILED — aborting re-clone. Your checkout and data are untouched."
    else
        log "  no okuro binary under $INSTALL_DIR — nothing installed to back up, continuing"
    fi

    staging="${INSTALL_DIR}.reclone-${ts}"
    retired="${INSTALL_DIR}.old-${ts}"
    [ -e "$staging" ] && die "staging path already exists: $staging — remove it and re-run"

    log "  cloning the new lineage from $REPO_URL ($branch)…"
    git clone --branch "$branch" "$REPO_URL" "$staging" \
        || die "git clone failed — nothing was changed; your checkout is intact"

    # Two renames inside one parent directory: the worst an interrupted swap
    # can leave behind is a recoverable sibling. Leave the tree first — our
    # cwd is about to be renamed away.
    cd "$(dirname "$INSTALL_DIR")" || die "cannot enter $(dirname "$INSTALL_DIR")"
    mv "$INSTALL_DIR" "$retired" || die "could not move the old checkout aside — nothing changed"
    if ! mv "$staging" "$INSTALL_DIR"; then
        mv "$retired" "$INSTALL_DIR" || log "RESTORE FAILED — your old checkout is at $retired"
        die "could not move the new clone into place — old checkout restored"
    fi
    cd "$INSTALL_DIR" || die "cannot enter $INSTALL_DIR"

    log "  old checkout kept at: $retired"
    log "  .venv and the built UI are regenerated by the installer; anything else"
    log "  you kept in that directory is still there. Delete it yourself once the"
    log "  new install checks out."
    # --reinstall: the fresh clone has no .venv, and with data present in
    # ~/.okuro install.sh converges (migrate → services → doctor) instead of
    # relaunching the wizard.
    exec bash ./install.sh --reinstall
else
    step "Installing okuro into $INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR" || die "git clone failed — check your network"
    cd "$INSTALL_DIR" || die "cannot enter $INSTALL_DIR"
    exec bash ./install.sh
fi
