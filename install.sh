#!/usr/bin/env bash
# Okuro installer — clone the repo, run this, your browser opens the wizard.
#
#   git clone https://github.com/saxmode/okuro-ai.git
#   cd okuro-ai
#   ./install.sh
#
# Creates a local venv in ./.venv, pip-installs okuro in editable mode, then
# launches the wizard. Re-runnable: when an existing install is detected,
# routes to scripts/update.sh (pull → build-what-changed → restart → verify)
# so users don't have to choose between install and update. Pass --reinstall
# to force the full installer path. Designed for Linux + macOS. Windows
# not supported.
#
# Output: branded phase-by-phase with brand-green accents, one line of
# "why" per phase so users aren't guessing what happens or whether it
# stalled. Detailed command output is dimmed or redirected to an install
# log. Runs offline-safe for the subset it can (no network except pip).

set -euo pipefail

# TODO(audit P2): --upgrade and --check flags

# Lightweight no-op flags so smoke tests + CI can sanity-check the script
# without running the full installer. These run BEFORE any tee/log setup so
# they never touch the user's ~/.okuro directory. They emit a canonical
# install_finished event to a temp jsonl when --emit-finished is also set,
# but the primary contract is just: exit 0, no side effects.
case "${1:-}" in
    --help|-h)
        cat <<'EOF'
Usage: ./install.sh [--reinstall] [--help]

Installs okuro into ./.venv and launches the wizard. Re-runnable.
When an existing install is detected, routes to scripts/update.sh
(pull → build-what-changed → restart → verify). Pass --reinstall
to force the full installer path.

Diagnostic logs land in ~/.okuro/install/install-<timestamp>/.

Flags:
    --reinstall  Skip auto-route to update.sh; run the full installer.
    --help, -h   Show this help and exit 0.

For full upgrade/check flags see TODO(audit P2) in install.sh.
EOF
        # Emit canonical finished event to a smoke-test JSONL if requested via
        # env var. Used by tests/test_install_sh_smoke.sh. Default is no-op so
        # users running --help don't get a stray file.
        if [ -n "${OKURO_INSTALL_SMOKE_JSONL:-}" ]; then
            mkdir -p "$(dirname "$OKURO_INSTALL_SMOKE_JSONL")"
            printf '{"event":"install_finished","ok":true,"mode":"help"}\n' \
                >> "$OKURO_INSTALL_SMOKE_JSONL"
        fi
        exit 0
        ;;
esac

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

# ─── auto-route to update.sh when already installed ────────────────────────
# Single entry point: users run ./install.sh whether it's day 1 or day 100.
# If we detect an existing install (venv, service unit, or okuro on PATH),
# defer to scripts/update.sh which knows how to pull, rebuild only what
# changed, restart services, and verify. The full installer path is heavier
# (deps re-resolve, services re-installed) and rarely what a returning user
# wants. --reinstall forces the installer path; useful after a wipe.
SKIP_AUTO_ROUTE=0
_FORWARD_ARGS=()
for _arg in "$@"; do
    case "$_arg" in
        --reinstall|--force-install) SKIP_AUTO_ROUTE=1 ;;
        *) _FORWARD_ARGS+=("$_arg") ;;
    esac
done
set -- "${_FORWARD_ARGS[@]+"${_FORWARD_ARGS[@]}"}"
unset _FORWARD_ARGS _arg

if [ "$SKIP_AUTO_ROUTE" = "0" ] && [ -x "$REPO_DIR/scripts/update.sh" ]; then
    if [ -x "$REPO_DIR/venv/bin/okuro" ] \
        || [ -x "$REPO_DIR/.venv/bin/okuro" ] \
        || [ -f "$HOME/.config/systemd/user/okuro-orchestrator.service" ] \
        || [ -f "$HOME/Library/LaunchAgents/com.okuro.orchestrator.plist" ] \
        || command -v okuro >/dev/null 2>&1; then
        printf '[install] existing install detected — routing to scripts/update.sh\n'
        printf '[install] (pass --reinstall to force a fresh install)\n'
        exec "$REPO_DIR/scripts/update.sh" "$@"
    fi
fi

# Standardize on .venv (matches scripts/update.sh's first-preference and the
# dev convention). Older installs created ./venv — the auto-route above still
# detects those, and update.sh resolves both, so existing setups keep working.
VENV="$REPO_DIR/.venv"
PY_MIN_MAJOR=3
PY_MIN_MINOR=11
NODE_MIN_MAJOR=18
UNAME="$(uname -s 2>/dev/null || echo unknown)"

# ─── branding ──────────────────────────────────────────────────────────────
# Truecolor sequences land on every modern terminal (macOS Terminal, iTerm2,
# GNOME Terminal, kitty, Alacritty, WezTerm, tilix, ghostty). Fall back to
# no-color if stdout isn't a TTY (CI, redirects) so log files stay readable.

if [ -t 1 ] && [ "${NO_COLOR:-}" = "" ]; then
    C_ACCENT=$'\033[38;2;34;197;94m'   # #22c55e — brand green
    C_DIM=$'\033[2m'
    C_BOLD=$'\033[1m'
    C_RED=$'\033[38;2;239;68;68m'
    C_RESET=$'\033[0m'
else
    C_ACCENT=""; C_DIM=""; C_BOLD=""; C_RED=""; C_RESET=""
fi

# Phase counter + timing. PHASE_TOTAL must match the actual number of
# `phase` calls below (1=python, 2=node, 3=venv, 4=install, 5=build-ui,
# 6=webview, 7=launcher, 8=cli-on-path, 9=ready-check, 10=verification,
# 11=launch).
PHASE_TOTAL=11
PHASE_NUM=0
PHASE_START=0
PHASE_NAME=""        # current phase name — used by record_event
INSTALL_START=$(date +%s)

# ─── diagnostic log ────────────────────────────────────────────────────────
# Every install run drops a timestamped directory under ~/.okuro/install/
# containing a tee'd terminal transcript, a structured jsonl event stream,
# and the pip output. Designed for "I tried it on my Mac and something
# was off" — the user can paste back the install.jsonl summary and we can
# see exactly where reality diverged from the expected sequence.
#
# Layout:
#   ~/.okuro/install/install-YYYYMMDD-HHMMSS/
#       install.log    — full stdout+stderr (with ANSI colours)
#       install.jsonl  — one JSON object per phase + per fact + per check
#       pip.log        — verbose pip output (failure tail)

LOG_TS="$(date +%Y%m%d-%H%M%S)"
OKURO_INSTALL_DIR="$HOME/.okuro/install"
INSTALL_RUN_DIR="$OKURO_INSTALL_DIR/install-$LOG_TS"
mkdir -p "$INSTALL_RUN_DIR"
INSTALL_TRANSCRIPT="$INSTALL_RUN_DIR/install.log"
INSTALL_JSONL="$INSTALL_RUN_DIR/install.jsonl"
INSTALL_LOG="$INSTALL_RUN_DIR/pip.log"   # consumed later by the pip step

# Mirror everything to the transcript file via process substitution. tee
# is bash 3.2-safe (process substitution `>(...)` works on macOS default
# bash). Colour codes survive — easier to re-read; strip with
# `sed 's/\x1b\[[0-9;]*m//g'` if pasted into a text editor that doesn't
# render ANSI.
#
# Save the original fd 1/2 to fd 3/4 BEFORE the tee redirect so _flush_logs
# can restore them on exit. Without this, `wait` would hang on tee because
# tee's stdin (the script's stdout) never closes — leaving install.log
# truncated and the canonical install_finished event missing from
# install.jsonl. See audit-2026-04-26 finding #27.
#
# fd 5 holds the original stdin for the same reason: every phase below runs
# with stdin closed (see the /dev/null redirect), but the app we exec into at
# [11/11] is interactive and must get the real terminal back. _flush_logs
# restores all three.
exec 3>&1 4>&2 5<&0
exec > >(tee -a "$INSTALL_TRANSCRIPT") 2>&1

# No phase of this installer may ever ask a question — install.sh itself never
# reads stdin, so nothing here needs it. Closing it means a subprocess that
# tries to prompt gets EOF and fails fast instead of blocking forever.
#
# This is a hard guarantee, not a courtesy: a prompt here is close to
# undiagnosable. Two fresh Macs hung at [5/11] for exactly this reason —
# corepack asked "? Do you want to continue? [Y/n]" on a cold cache and waited
# on stdin, while the output filter's block buffer swallowed the question. The
# terminal just sat there, mute, forever. Any future tool that grows a prompt
# gets caught here instead of shipping that experience again.
exec < /dev/null

# Counters used by the verification phase.
CHECK_PASS=0
CHECK_FAIL=0

# Indent a piped stream by four spaces, one line at a time, as it arrives.
#
# Deliberately NOT `sed 's/^/    /'`. Our stdout is the transcript tee's pipe
# rather than a tty, so sed switches to 4KB block buffering and anything
# shorter than that never reaches the screen while a step is still running —
# progress, stalls, and the error right before a hang all vanish. bash's read
# loop is line-buffered by construction and portable, which sed's flags are
# not: GNU sed spells it -u, BSD/macOS sed spells it -l.
#
# `|| [ -n "$line" ]` flushes a trailing line that has no newline — the exact
# shape of a prompt, and the thing whose disappearance made the corepack hang
# so hard to see.
indent() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        printf '    %s\n' "$line"
    done
}

# JSON-escape one string. Handles backslash, double-quote, newline, tab.
# Keeps the implementation small — we don't need a full RFC-8259 escaper
# because every value passing through is install.sh-controlled.
_json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' \
        | awk 'BEGIN{ORS=""} {if (NR>1) printf "\\n"; print $0}'
}

# Append one structured record to the jsonl stream. Failures here are
# non-fatal (set -u + pipe crash would mask the real install failure).
record_event() {
    # record_event TYPE NAME STATUS [DURATION_S=0] [DETAIL=""]
    local type="$1" name="$2" status="$3" duration="${4:-0}" detail="${5:-}"
    {
        printf '{"ts":"%s","type":"%s","phase":"%s","name":"%s","status":"%s","duration_s":%s,"detail":"%s"}\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            "$type" "$PHASE_NAME" "$name" "$status" "$duration" \
            "$(_json_escape "$detail")" \
            >> "$INSTALL_JSONL"
    } 2>/dev/null || true
}

# Append a raw JSON line to the jsonl stream. Used for canonical
# install_finished events so the trailing record is a known shape that
# tooling can grep for unambiguously: {"event":"install_finished",...}.
# Failures non-fatal so a disk-full at the end never masks the real exit code.
_emit_jsonl() {
    {
        printf '%s\n' "$1" >> "$INSTALL_JSONL"
    } 2>/dev/null || true
}

# Flush both the tee'd transcript and the jsonl stream before exit. Without
# this, the tee subprocess can outlive the script on early exit (set -e
# trap, exec replacement) and leave install.log truncated. See audit-2026-
# 04-26 finding #27.
#
# Restoring fd 1/2 to the originals (saved at fd 3/4 above) closes tee's
# stdin so it can drain and exit. `wait` then blocks until tee is gone.
# `sync` nudges the kernel to commit the file buffers to disk.
#
# stdin comes back from fd 5 at the same time: the phases ran with it closed,
# but the wizard we exec into at [11/11] needs the real terminal to drive
# onboarding.
_flush_logs() {
    exec >&3 2>&4 <&5 || true
    wait 2>/dev/null || true
    sync 2>/dev/null || true
}

# Trap handler — emits the canonical install_finished event with ok=false
# AND the failing line number BEFORE flushing logs. Without this, abort
# leaves install.jsonl missing the most important diagnostic record (the
# one the user is told to paste back). The verification probe re-reads
# the last line of install.jsonl and warns the user via stderr if the
# event is missing — covers the worst case where the disk filled up
# mid-emit.
_on_error() {
    local rc=$?
    local lineno="${1:-0}"
    fail_msg "install.sh failed at line ${lineno} (rc=${rc}) — see output above"
    _emit_jsonl '{"event":"install_finished","ok":false,"rc":'"$rc"',"lineno":'"$lineno"'}'
    _flush_logs
    # Verification probe — confirm the canonical event actually landed.
    # Use fd 4 (the original stderr saved before tee) so the warning is
    # visible to the user even after _flush_logs reassigned stderr.
    if [ -r "$INSTALL_JSONL" ]; then
        if ! tail -n 1 "$INSTALL_JSONL" 2>/dev/null | grep -q '"event":"install_finished"'; then
            printf 'WARNING: install.jsonl truncated; please paste both install.log AND install.jsonl when reporting.\n' >&4 2>/dev/null \
                || printf 'WARNING: install.jsonl truncated; please paste both install.log AND install.jsonl when reporting.\n' >&2
        fi
    fi
    exit "$rc"
}

# Write the run header — system metadata captured up front so even a
# crash during phase 1 leaves enough context to diagnose.
{
    record_event "header" "install_started" "ok" 0 "okuro install.sh"
    record_event "env" "uname" "ok" 0 "$UNAME"
    record_event "env" "arch" "ok" 0 "$(uname -m 2>/dev/null || echo unknown)"
    record_event "env" "kernel" "ok" 0 "$(uname -r 2>/dev/null || echo unknown)"
    record_event "env" "shell" "ok" 0 "${SHELL:-unknown}"
    record_event "env" "user" "ok" 0 "$(whoami 2>/dev/null || echo unknown)"
    record_event "env" "repo_dir" "ok" 0 "$REPO_DIR"
    record_event "env" "venv_dir" "ok" 0 "$VENV"
    if [ "$UNAME" = "Darwin" ]; then
        record_event "env" "macos_version" "ok" 0 "$(sw_vers -productVersion 2>/dev/null || echo unknown)"
        record_event "env" "macos_build" "ok" 0 "$(sw_vers -buildVersion 2>/dev/null || echo unknown)"
        record_event "env" "homebrew" "ok" 0 "$(command -v brew 2>/dev/null || echo not-installed)"
    fi
}

# ─── helpers ───────────────────────────────────────────────────────────────

phase() {
    # phase "Name" "why this step exists"
    PHASE_NUM=$((PHASE_NUM + 1))
    PHASE_START=$(date +%s)
    PHASE_NAME="$1"
    printf "\n%s▸%s %s[%d/%d] %s%s\n" \
        "$C_ACCENT" "$C_RESET" "$C_BOLD" "$PHASE_NUM" "$PHASE_TOTAL" "$1" "$C_RESET"
    if [ -n "${2:-}" ]; then
        printf "  %swhy:%s %s%s%s\n" "$C_DIM" "$C_RESET" "$C_DIM" "$2" "$C_RESET"
    fi
    record_event "phase" "$1" "started" 0 "${2:-}"
}

phase_done() {
    # phase_done ["summary line"]
    local elapsed=$(( $(date +%s) - PHASE_START ))
    local msg="${1:-done}"
    printf "  %s✓%s %s %s(%ds)%s\n" \
        "$C_ACCENT" "$C_RESET" "$msg" "$C_DIM" "$elapsed" "$C_RESET"
    record_event "phase" "$PHASE_NAME" "ok" "$elapsed" "$msg"
}

step() {
    # step "detail line" — action log dimmed so phase/done lines stand out
    printf "  %s·%s %s%s%s\n" "$C_DIM" "$C_RESET" "$C_DIM" "$1" "$C_RESET"
}

warn_msg() {
    printf "  %s!%s %s\n" "$C_ACCENT" "$C_RESET" "$1"
    record_event "warn" "${PHASE_NAME:-pre-phase}" "warn" 0 "$1"
}

fail_msg() {
    printf "\n  %s✗ %s%s\n" "$C_RED" "$1" "$C_RESET" >&2
    record_event "fail" "${PHASE_NAME:-pre-phase}" "failed" 0 "$1"
}

# Verification helper — used by the `[9/10] Verification` phase. Records a
# named check with pass/fail status to the jsonl stream AND prints a
# colour-coded line so the user can see results in the terminal.
record_check() {
    # record_check NAME STATUS DETAIL  (status: ok | warn | fail)
    local name="$1" status="$2" detail="$3"
    record_event "check" "$name" "$status" 0 "$detail"
    case "$status" in
        ok)
            printf "  %s✓%s %s %s%s%s\n" \
                "$C_ACCENT" "$C_RESET" "$name" "$C_DIM" "$detail" "$C_RESET"
            CHECK_PASS=$((CHECK_PASS + 1))
            ;;
        warn)
            printf "  %s!%s %s %s%s%s\n" \
                "$C_ACCENT" "$C_RESET" "$name" "$C_DIM" "$detail" "$C_RESET"
            ;;
        fail|*)
            printf "  %s✗%s %s %s%s%s\n" \
                "$C_RED" "$C_RESET" "$name" "$C_DIM" "$detail" "$C_RESET"
            CHECK_FAIL=$((CHECK_FAIL + 1))
            ;;
    esac
}

banner() {
    cat <<EOF

${C_ACCENT}╭──────────────────────────────────────────────╮${C_RESET}
${C_ACCENT}│${C_RESET}  ${C_BOLD}OKURO${C_RESET} ${C_DIM}—${C_RESET} personal AI agent infrastructure  ${C_ACCENT}│${C_RESET}
${C_ACCENT}╰──────────────────────────────────────────────╯${C_RESET}

  ${C_DIM}installing to${C_RESET} ${REPO_DIR}
  ${C_DIM}venv at${C_RESET}       ${VENV}
  ${C_DIM}log dir${C_RESET}       ${INSTALL_RUN_DIR}

EOF
}

summary() {
    local total=$(( $(date +%s) - INSTALL_START ))
    record_event "summary" "install_finished" "ok" "$total" \
        "checks_pass=$CHECK_PASS checks_fail=$CHECK_FAIL"
    # Canonical terminal record — same shape the trap emits with ok=false,
    # so any tooling can grep `tail -n1 install.jsonl` for `install_finished`.
    _emit_jsonl '{"event":"install_finished","ok":true,"checks_pass":'"$CHECK_PASS"',"checks_fail":'"$CHECK_FAIL"',"duration_s":'"$total"'}'
    printf "\n%s─────────────────────────────────────────────%s\n" "$C_ACCENT" "$C_RESET"
    printf "  %s✓ installed in %ds%s\n" "$C_ACCENT" "$total" "$C_RESET"
    if [ "$CHECK_FAIL" -gt 0 ]; then
        printf "  %s! verification: %d ok, %d failed%s\n" \
            "$C_RED" "$CHECK_PASS" "$CHECK_FAIL" "$C_RESET"
    else
        printf "  %s· verification: %d ok, 0 failed%s\n" \
            "$C_DIM" "$CHECK_PASS" "$C_RESET"
    fi
    printf "  %sdiagnostic log:%s %s\n" "$C_DIM" "$C_RESET" "$INSTALL_RUN_DIR"
    printf "  %sshare for debug:%s ${C_BOLD}cat %s/install.jsonl${C_RESET}\n" \
        "$C_DIM" "$C_RESET" "$INSTALL_RUN_DIR"
    printf "  %snext:%s the wizard opens in a moment (or run ${C_BOLD}okuro${C_RESET} again any time)\n" \
        "$C_DIM" "$C_RESET"
    printf "  %sheads-up:%s background services install when you FINISH the wizard. If you close it early, run ${C_BOLD}okuro init${C_RESET} to install them.\n" \
        "$C_DIM" "$C_RESET"
    printf "%s─────────────────────────────────────────────%s\n\n" "$C_ACCENT" "$C_RESET"
}

trap '_on_error $LINENO' ERR

# ─── banner + platform ─────────────────────────────────────────────────────

banner

# Homebrew PATH (macOS only) — when install.sh is launched from Finder / a
# .command dropper the shell inherits LaunchServices PATH (no /opt/homebrew,
# no /usr/local/bin). Sourcing brew shellenv here keeps the probes honest
# regardless of launch context. Safe on hosts without brew.
if [ "$UNAME" = "Darwin" ]; then
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)" || \
    eval "$(/usr/local/bin/brew shellenv 2>/dev/null)" || true
fi

# ─── install hints (used when a prereq is missing) ─────────────────────────

hint_python_install() {
    case "$UNAME" in
        Darwin) echo "  brew install python@3.12   # install Homebrew first: https://brew.sh" ;;
        Linux)
            if command -v apt >/dev/null 2>&1; then
                echo "  sudo apt install python3.12 python3.12-venv"
            elif command -v dnf >/dev/null 2>&1; then
                echo "  sudo dnf install python3.12"
            elif command -v pacman >/dev/null 2>&1; then
                echo "  sudo pacman -S python"
            else
                echo "  See https://www.python.org/downloads/"
            fi
            ;;
        *) echo "  See https://www.python.org/downloads/" ;;
    esac
}

hint_node_install() {
    case "$UNAME" in
        Darwin) echo "  brew install node          # or nvm: https://github.com/nvm-sh/nvm" ;;
        Linux)
            if command -v apt >/dev/null 2>&1; then
                echo "  sudo apt install nodejs npm"
                echo "  (if that gives you Node < 18, use nvm: https://github.com/nvm-sh/nvm)"
            elif command -v dnf >/dev/null 2>&1; then
                echo "  sudo dnf install nodejs"
            elif command -v pacman >/dev/null 2>&1; then
                echo "  sudo pacman -S nodejs npm"
            else
                echo "  See https://nodejs.org/"
            fi
            ;;
        *) echo "  See https://nodejs.org/" ;;
    esac
}

# pip will compile some deps from source on a fresh box (notably cryptography
# without a manylinux match, and pyobjc-framework-* on Apple Silicon if the
# wheel cache misses). Without a C compiler the failure is a 200-line .install.log
# trace. Probe up front so the user sees an actionable hint instead.
hint_cc_install() {
    case "$UNAME" in
        Darwin) echo "  xcode-select --install" ;;
        Linux)
            if command -v apt >/dev/null 2>&1; then
                echo "  sudo apt install build-essential"
            elif command -v dnf >/dev/null 2>&1; then
                echo "  sudo dnf groupinstall 'Development Tools'"
            elif command -v pacman >/dev/null 2>&1; then
                echo "  sudo pacman -S base-devel"
            elif command -v apk >/dev/null 2>&1; then
                echo "  sudo apk add build-base"
            else
                echo "  Install gcc/clang via your package manager."
            fi
            ;;
        *) echo "  Install gcc or clang." ;;
    esac
}

pick_python() {
    # Prefer 3.12/3.11 over 3.13: tree-sitter (code-graph/cAST) has no cp313
    # wheel and is gated to python<3.13, so 3.13 silently loses code-graph.
    # 3.13 still works and is accepted if it's all that's present.
    for candidate in python3.12 python3.11 python3.13 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)" 2>/dev/null; then
                echo "$candidate"
                return
            fi
        fi
    done
    return 1
}

# ─── [1/11] python ─────────────────────────────────────────────────────────

phase "Python environment" "okuro runs on Python 3.11+ — we use the newest one on your PATH"

if PYTHON="$(pick_python)"; then
    PY_VER="$("$PYTHON" --version 2>&1 | awk '{print $2}')"
    PY_PATH="$(command -v "$PYTHON" 2>/dev/null || echo "$PYTHON")"
    step "found ${PYTHON} (${PY_VER})"
    record_event "fact" "python_path" "ok" 0 "$PY_PATH"
    record_event "fact" "python_version" "ok" 0 "$PY_VER"
    # code-graph/cAST needs tree-sitter (no wheel for 3.13+) — warn so the
    # silent degrade is visible; cortex still works without code-graph.
    PY_MINOR_NUM="$("$PYTHON" -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)"
    if [ "$PY_MINOR_NUM" -ge 13 ] 2>/dev/null; then
        warn_msg "Python ${PY_VER}: code-graph/cAST chunking disabled (tree-sitter has no wheel for 3.13+). Install python@3.12 for full cortex search."
        record_event "fact" "code_graph" "warn" 0 "disabled on Python ${PY_VER}"
    fi
    phase_done "Python ${PY_VER}"
else
    fail_msg "Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+ not found on PATH"
    printf "\n  Install with:\n" >&2
    hint_python_install >&2
    echo >&2
    exit 1
fi

# ─── [2/11] node ───────────────────────────────────────────────────────────

phase "Node runtime" "the wizard installs Claude Code / Codex / Gemini CLIs via npm"

if command -v node >/dev/null 2>&1; then
    NODE_VERSION_STRING="$(node --version 2>/dev/null || echo '')"
    NODE_PATH="$(command -v node 2>/dev/null || echo '')"
    NODE_MAJOR="${NODE_VERSION_STRING#v}"
    NODE_MAJOR="${NODE_MAJOR%%.*}"
    if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -ge "$NODE_MIN_MAJOR" ] 2>/dev/null; then
        step "found node ${NODE_VERSION_STRING}"
        record_event "fact" "node_path" "ok" 0 "$NODE_PATH"
        record_event "fact" "node_version" "ok" 0 "$NODE_VERSION_STRING"
        phase_done "Node ${NODE_VERSION_STRING}"
    else
        record_event "fact" "node_version" "fail" 0 "$NODE_VERSION_STRING (need >= $NODE_MIN_MAJOR)"
        fail_msg "Node ${NODE_MIN_MAJOR}+ required, found ${NODE_VERSION_STRING:-unknown}"
        printf "\n  Install with:\n" >&2
        hint_node_install >&2
        echo >&2
        exit 1
    fi
else
    record_event "fact" "node_path" "fail" 0 "not on PATH"
    fail_msg "Node ${NODE_MIN_MAJOR}+ not found on PATH"
    printf "\n  Install with:\n" >&2
    hint_node_install >&2
    echo >&2
    exit 1
fi

# macOS Homebrew is advisory — some CLI installs prefer casks but the wizard
# always has an npm fallback. We surface presence for transparency only.
if [ "$UNAME" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
        BREW_VER="$(brew --version | head -1)"
        step "Homebrew: $BREW_VER"
        record_event "fact" "homebrew_version" "ok" 0 "$BREW_VER"
        record_event "fact" "homebrew_prefix" "ok" 0 "$(brew --prefix 2>/dev/null || echo unknown)"
    else
        step "Homebrew: not installed (optional — wizard will use npm for every CLI install)"
        record_event "fact" "homebrew_version" "warn" 0 "not installed"
    fi
fi

# ─── [3/11] venv ───────────────────────────────────────────────────────────

phase "Virtual environment" "isolate okuro's deps from your system Python — rm -rf .venv to reset"

if [ ! -x "$VENV/bin/python" ]; then
    step "creating venv at ${VENV}"
    "$PYTHON" -m venv "$VENV" 2>&1 | indent || {
        record_event "fact" "venv_create" "fail" 0 "venv module failed"
        fail_msg "venv creation failed"
        exit 1
    }
    record_event "fact" "venv_create" "ok" 0 "created at $VENV"
    phase_done "venv created"
else
    step "reusing existing venv"
    record_event "fact" "venv_create" "ok" 0 "reused at $VENV"
    phase_done "venv ready"
fi

VPY="$VENV/bin/python"
"$VPY" -m pip install --quiet --upgrade pip >/dev/null

# ─── [4/11] package install ────────────────────────────────────────────────

phase "Install okuro" "editable install — code edits picked up without re-bundling (~130 MB model on first run)"

# Pre-flight: pip will compile cryptography (and a few others) from source on
# a fresh box where the wheel cache misses. Without a C compiler the failure
# surfaces as a long, intimidating tail of pip.log. Probe first so we
# can print a one-line install hint instead of a build-error wall.
if ! "$VPY" -c "import okuro" 2>/dev/null; then
    CC_FOUND=""
    for cc_candidate in clang gcc cc; do
        if command -v "$cc_candidate" >/dev/null 2>&1; then
            CC_FOUND="$(command -v "$cc_candidate")"
            break
        fi
    done
    if [ -z "$CC_FOUND" ]; then
        # All base deps ship prebuilt wheels (incl. macosx arm64), so pip
        # usually needs no compiler. Only warn — let pip proceed and surface
        # a real build need itself if the wheel cache genuinely misses.
        record_event "fact" "c_toolchain" "warn" 0 "no cc/clang/gcc on PATH"
        warn_msg "no C compiler on PATH — fine if every dep has a wheel (the common case). If a source build is needed, pip will fail; then install one:"
        hint_cc_install >&2
        echo >&2
    else
        record_event "fact" "c_toolchain" "ok" 0 "$CC_FOUND"
    fi
    step "first-time install — live output below (also captured to ${INSTALL_LOG})"
    step "downloading deps + embedding model (one-time, ~130 MB)"
    printf "  %s%s%s\n" "$C_DIM" "─── pip output ───────────────────────────────────────────" "$C_RESET"
    # Stream pip output live AND mirror it to pip.log via tee. PYTHONUNBUFFERED
    # disables Python's stdio buffering so per-package "Downloading" lines
    # appear as they happen rather than in one big flush at the end. set -o
    # pipefail (already on at script top) makes the if-test honour pip's exit
    # code even though tee returns 0 — so a pip failure still fails the step.
    if ! PYTHONUNBUFFERED=1 "$VPY" -m pip install --progress-bar on -e "$REPO_DIR" 2>&1 | tee "$INSTALL_LOG"; then
        printf "  %s%s%s\n" "$C_DIM" "──────────────────────────────────────────────────────────" "$C_RESET"
        record_event "fact" "pip_install" "fail" 0 "pip install failed (see pip.log)"
        fail_msg "pip install failed — full output already shown above; tail also captured to ${INSTALL_LOG}"
        exit 1
    fi
    printf "  %s%s%s\n" "$C_DIM" "──────────────────────────────────────────────────────────" "$C_RESET"
    # Extract the "Successfully installed" line if present for a concise summary.
    installed_line="$(grep -m1 '^Successfully installed' "$INSTALL_LOG" 2>/dev/null || true)"
    if [ -n "$installed_line" ]; then
        pkg_count=$(echo "$installed_line" | awk '{print NF - 2}')
        record_event "fact" "pip_install" "ok" 0 "first-time install: $pkg_count packages"
        base_done_msg="installed ${pkg_count} packages"
    else
        record_event "fact" "pip_install" "ok" 0 "first-time install (count parse failed)"
        base_done_msg="okuro installed"
    fi

    # ─── embed extras (sentence-transformers + torch) ─────────────────────
    # Without this, cortex semantic search and the okuro-embed plist sit in
    # "degraded" mode (the in-process fallback raises EmbeddingsUnavailable
    # because sentence-transformers isn't importable). Pre-fix we asked
    # users to "complete the semantic search step in okuro init", but that
    # step never existed — install report 2026-04-28. Now the installer
    # picks the right extras for this hardware and pulls them automatically.
    EMBED_EXTRAS=""
    EMBED_INDEX_FLAG=""
    ARCH="$(uname -m 2>/dev/null || echo unknown)"
    if [ "$UNAME" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
        EMBED_EXTRAS="embed-apple"   # MPS built into PyTorch on macOS arm64
    elif [ "$UNAME" = "Darwin" ]; then
        EMBED_EXTRAS="embed-cpu"
        EMBED_INDEX_FLAG="--index-url https://download.pytorch.org/whl/cpu"
    else
        # Linux default: CPU wheels (smaller; ~250 MB instead of ~2 GB CUDA).
        # Users with NVIDIA can swap to embed-cuda manually after install.
        EMBED_EXTRAS="embed-cpu"
        EMBED_INDEX_FLAG="--index-url https://download.pytorch.org/whl/cpu"
    fi
    step "installing embed extras (${EMBED_EXTRAS}) — sentence-transformers + torch for semantic search"
    EMBED_LOG="$INSTALL_RUN_DIR/pip-embed.log"
    # --line-buffered: this step downloads torch (~250 MB) and runs for
    # minutes. Without it grep block-buffers at 4KB and the terminal shows
    # nothing until the whole install is done — indistinguishable from a hang.
    # Portable: both GNU and BSD/macOS grep accept the flag.
    if PYTHONUNBUFFERED=1 "$VPY" -m pip install -e "${REPO_DIR}[${EMBED_EXTRAS}]" $EMBED_INDEX_FLAG 2>&1 | tee "$EMBED_LOG" | grep --line-buffered -E "^(Collecting|Successfully|ERROR|WARNING)" || true; then
        # tee preserves the full log; we only print the headline lines to the terminal.
        if grep -q "^Successfully installed" "$EMBED_LOG" 2>/dev/null || \
           "$VPY" -c "import sentence_transformers" >/dev/null 2>&1; then
            record_event "fact" "embed_extras" "ok" 0 "${EMBED_EXTRAS} installed"
            step "embed extras ready (${EMBED_EXTRAS})"
        else
            record_event "fact" "embed_extras" "warn" 0 "${EMBED_EXTRAS} install: outcome unclear (see pip-embed.log)"
            warn_msg "embed extras install finished but sentence_transformers still not importable; semantic search will be degraded"
            warn_msg "manual retry: ${C_DIM}${VPY} -m pip install -e '${REPO_DIR}[${EMBED_EXTRAS}]' ${EMBED_INDEX_FLAG}${C_RESET}"
        fi
    else
        record_event "fact" "embed_extras" "fail" 0 "${EMBED_EXTRAS} install failed"
        warn_msg "embed extras install FAILED — okuro is usable but semantic search / cortex will be degraded"
        warn_msg "manual retry: ${C_DIM}${VPY} -m pip install -e '${REPO_DIR}[${EMBED_EXTRAS}]' ${EMBED_INDEX_FLAG}${C_RESET}"
    fi

    # ─── voice extra (faster-whisper — local dictation STT) ───────────────
    # Without this, okuro-notes voice dictation fails with "local streaming STT
    # unavailable": whisper_stream.is_available() returns False because
    # faster-whisper isn't importable. A plain `pip install -e .` does NOT pull
    # optional extras, so the daemon shipped mute (dictation report 2026-07-04).
    # faster-whisper is CPU-friendly (~75 MB + ctranslate2); the STT tier system
    # upgrades to GPU large-v3 at runtime when a non-display GPU is present.
    step "installing voice extra (faster-whisper) — local dictation STT"
    VOICE_LOG="$INSTALL_RUN_DIR/pip-voice.log"
    if PYTHONUNBUFFERED=1 "$VPY" -m pip install -e "${REPO_DIR}[voice]" 2>&1 | tee "$VOICE_LOG" | grep --line-buffered -E "^(Collecting|Successfully|ERROR|WARNING)" || true; then
        if grep -q "^Successfully installed" "$VOICE_LOG" 2>/dev/null || \
           "$VPY" -c "import faster_whisper" >/dev/null 2>&1; then
            record_event "fact" "voice_extra" "ok" 0 "faster-whisper installed"
            step "voice extra ready (dictation enabled)"
        else
            record_event "fact" "voice_extra" "warn" 0 "voice install: outcome unclear (see pip-voice.log)"
            warn_msg "voice extra install finished but faster_whisper still not importable; dictation will be unavailable"
            warn_msg "manual retry: ${C_DIM}${VPY} -m pip install -e '${REPO_DIR}[voice]'${C_RESET}"
        fi
    else
        record_event "fact" "voice_extra" "fail" 0 "voice extra install failed"
        warn_msg "voice extra install FAILED — okuro is usable but note dictation will be unavailable"
        warn_msg "manual retry: ${C_DIM}${VPY} -m pip install -e '${REPO_DIR}[voice]'${C_RESET}"
    fi

    phase_done "$base_done_msg"
else
    step "refreshing (cheap — already installed)"
    # Refresh is fast and usually silent — keep --quiet here, but route through
    # tee so a slow refresh (e.g. metadata fetch from PyPI on a flaky network)
    # surfaces SOMETHING instead of dead silence.
    if ! PYTHONUNBUFFERED=1 "$VPY" -m pip install --quiet -e "$REPO_DIR" 2>&1 | tee "$INSTALL_LOG"; then
        record_event "fact" "pip_install" "fail" 0 "pip refresh failed"
        fail_msg "pip refresh failed — see ${INSTALL_LOG}"
        exit 1
    fi
    # On refresh runs, only retry embed extras if sentence_transformers is missing.
    # This handles the case where a previous install failed to install extras.
    if ! "$VPY" -c "import sentence_transformers" >/dev/null 2>&1; then
        ARCH="$(uname -m 2>/dev/null || echo unknown)"
        if [ "$UNAME" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
            EMBED_EXTRAS="embed-apple"; EMBED_INDEX_FLAG=""
        elif [ "$UNAME" = "Darwin" ]; then
            EMBED_EXTRAS="embed-cpu"; EMBED_INDEX_FLAG="--index-url https://download.pytorch.org/whl/cpu"
        else
            EMBED_EXTRAS="embed-cpu"; EMBED_INDEX_FLAG="--index-url https://download.pytorch.org/whl/cpu"
        fi
        step "embed extras missing — installing ${EMBED_EXTRAS}"
        EMBED_LOG="$INSTALL_RUN_DIR/pip-embed.log"
        if ! PYTHONUNBUFFERED=1 "$VPY" -m pip install -e "${REPO_DIR}[${EMBED_EXTRAS}]" $EMBED_INDEX_FLAG > "$EMBED_LOG" 2>&1; then
            warn_msg "embed extras install failed on refresh — cortex / semantic search degraded"
        fi
    fi
    phase_done "okuro refreshed"
fi

# ─── [5/11] build the UI ───────────────────────────────────────────────────

phase "Build the UI" "compile the web app the window renders — skip this and the dashboard is blank"

# The SPA dist is gitignored, hatch doesn't build it, and the wheel doesn't
# ship it — so a fresh clone has no UI until we build it here. Delegates to the
# shared builder (also used by update.sh) so the build lives in exactly one
# place. pipefail makes the `if` honour the build's exit code through indent.
if bash "$REPO_DIR/scripts/build-frontend.sh" "$REPO_DIR/src/okuro/web/frontend" 2>&1 | indent; then
    record_event "fact" "frontend_build" "ok" 0 "dist built"
    phase_done "UI built"
else
    record_event "fact" "frontend_build" "fail" 0 "frontend build failed"
    fail_msg "frontend build failed — see output above (needs Node ${NODE_MIN_MAJOR}+ and pnpm)"
    exit 1
fi

# ─── [6/11] webview bindings ───────────────────────────────────────────────

phase "Native window bindings" "renders the dashboard as a real app window instead of a browser tab"

WEBVIEW_OK=0
if [ "$UNAME" = "Linux" ]; then
    # pywebview on Linux shells out to system WebKitGTK via Python GObject
    # bindings. These ship as apt/dnf/pacman packages and can't be pip-installed
    # cleanly. A .pth shim lets the venv resolve against the system build
    # without recreating it with --system-site-packages.
    #
    # WebKit2 ships in two ABI generations: 4.1 on Ubuntu 24.04+ / Fedora 39+
    # / Arch current, and 4.0 on Ubuntu 22.04 LTS (still the most-deployed LTS
    # in 2026). Probe 4.1 first, fall back to 4.0 — same Python API, different
    # underlying libsoup. Without the dual-probe, 22.04 users silently fall
    # through to browser-fallback even if they followed the apt-install hint.
    SITE="$("$VPY" -c 'import site, sys; print(next(p for p in site.getsitepackages() if p.startswith(sys.prefix)))' 2>/dev/null || echo '')"
    if [ -n "$SITE" ] && [ -d /usr/lib/python3/dist-packages ]; then
        echo '/usr/lib/python3/dist-packages' > "$SITE/okuro-system-gi.pth"
    fi
    WEBKIT_VER=""
    if "$VPY" -c "import gi; gi.require_version('WebKit2', '4.1'); from gi.repository import WebKit2" 2>/dev/null; then
        WEBKIT_VER="4.1"
    elif "$VPY" -c "import gi; gi.require_version('WebKit2', '4.0'); from gi.repository import WebKit2" 2>/dev/null; then
        WEBKIT_VER="4.0"
    fi
    if [ -n "$WEBKIT_VER" ]; then
        step "WebKitGTK ${WEBKIT_VER} bindings present"
        record_event "fact" "webkit_gtk" "ok" 0 "version $WEBKIT_VER"
        WEBVIEW_OK=1
    else
        record_event "fact" "webkit_gtk" "warn" 0 "missing — using browser fallback"
        step "system WebKitGTK bindings missing — falling back to default browser"
        warn_msg "for a real app window install these and re-run:"
        if command -v apt >/dev/null 2>&1; then
            # Detect which WebKit2 generation is available in apt and tell the
            # user the right package name for THEIR distro.
            if apt-cache show gir1.2-webkit2-4.1 >/dev/null 2>&1; then
                printf "      ${C_DIM}sudo apt install python3-gi python3-cairo gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0${C_RESET}\n"
            elif apt-cache show gir1.2-webkit2-4.0 >/dev/null 2>&1; then
                printf "      ${C_DIM}sudo apt install python3-gi python3-cairo gir1.2-webkit2-4.0 libwebkit2gtk-4.0-37${C_RESET}\n"
            else
                printf "      ${C_DIM}sudo apt install python3-gi python3-cairo gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0${C_RESET}\n"
                printf "      ${C_DIM}(if 4.1 is unavailable on your distro, try the -4.0 variants)${C_RESET}\n"
            fi
        elif command -v dnf >/dev/null 2>&1; then
            printf "      ${C_DIM}sudo dnf install python3-gobject python3-cairo webkit2gtk4.1${C_RESET}\n"
        elif command -v pacman >/dev/null 2>&1; then
            printf "      ${C_DIM}sudo pacman -S python-gobject python-cairo webkit2gtk-4.1${C_RESET}\n"
        else
            printf "      ${C_DIM}see https://pywebview.flowrl.com/guide/installation.html${C_RESET}\n"
        fi
    fi
fi

if [ "$UNAME" = "Darwin" ]; then
    # pyobjc WebKit/Cocoa drives the native WKWebView window. pywebview
    # declares them as extras, not hard deps, so a clean pip install from
    # PyPI can land without them. Surface the gap here instead of at first
    # click.
    if "$VPY" -c "import WebKit; import Cocoa" >/dev/null 2>&1; then
        step "pyobjc WebKit/Cocoa bindings present"
        record_event "fact" "pyobjc" "ok" 0 "WebKit + Cocoa imported"
        WEBVIEW_OK=1
    else
        step "pyobjc bindings missing — falling back to default browser"
        record_event "fact" "pyobjc" "warn" 0 "missing — using browser fallback"
        warn_msg "reinstall to pick up Darwin-gated deps: ${C_DIM}${VENV}/bin/pip install -e .${C_RESET}"
    fi
fi

if [ "$WEBVIEW_OK" = "1" ]; then
    phase_done "native window ready"
else
    phase_done "browser fallback"
fi

# ─── [7/11] desktop launcher ───────────────────────────────────────────────

phase "Desktop launcher" "add okuro to your app grid / Dock so you don't need a terminal to open it"

ICON_PNG="$REPO_DIR/src/okuro/web/frontend/public/icons/icon-512.png"

if [ "$UNAME" = "Linux" ]; then
    APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    ICONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/512x512/apps"
    DESKTOP_FILE="$APPS_DIR/okuro.desktop"
    ICON_DEST="$ICONS_DIR/okuro.png"

    if [ -f "$ICON_PNG" ]; then
        mkdir -p "$APPS_DIR" "$ICONS_DIR"
        cp "$ICON_PNG" "$ICON_DEST"
        cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Okuro
GenericName=Agent Infrastructure
Comment=Agent-native OS — dashboard, bridge, cortex, sessions
Exec=$VENV/bin/okuro
Icon=okuro
Terminal=false
Categories=Development;
StartupNotify=true
StartupWMClass=Okuro
Keywords=agent;ai;mcp;dashboard;okuro;
EOF
        chmod 644 "$DESKTOP_FILE"
        command -v update-desktop-database >/dev/null 2>&1 && \
            update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
        command -v gtk-update-icon-cache >/dev/null 2>&1 && \
            gtk-update-icon-cache -q -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
        step ".desktop entry at ${DESKTOP_FILE}"
        phase_done "launcher registered"
    else
        step "icon missing — skipping launcher registration"
        phase_done "skipped"
    fi
elif [ "$UNAME" = "Darwin" ] && [ -f "$ICON_PNG" ]; then
    APP_DIR="$HOME/Applications/Okuro.app"
    MACOS_DIR="$APP_DIR/Contents/MacOS"
    RES_DIR="$APP_DIR/Contents/Resources"

    # Skeleton only — Info.plist, okuro.icns, launcher binary, pyvenv.cfg,
    # lib symlink, renamed Python copies and the LaunchServices refresh
    # are fully owned by ``okuro.system.interpreter.ensure_okuro_interpreter``.
    # That single helper means: every service install self-heals the
    # bundle, so a half-finished install.sh run, a manual edit, or a
    # stale icns on disk all repair on the next ``okuro service install``.
    # Centralising this also gets us the C launcher build (clang +
    # python3-config) which install.sh's bash heredocs couldn't do
    # cleanly without growing into a mini build system. See
    # ``okuro.system.interpreter`` for the rationale and the framework-
    # forwarding background.
    mkdir -p "$MACOS_DIR" "$RES_DIR"
    "$VPY" -c "from okuro.system.interpreter import ensure_okuro_interpreter; ensure_okuro_interpreter('okuro-cli')" \
        || warn_msg "could not materialize okuro-cli inside Okuro.app — first .app click may fall back to system python"
    touch "$APP_DIR"

    # Strip Gatekeeper quarantine so first-click from Finder / Spotlight /
    # Launchpad doesn't hit "cannot be opened because Apple cannot check it".
    # We strip it from the .app, the venv's `okuro` script (the thing the
    # bundle execs), AND the repo root — pip's editable install runs code
    # straight out of the repo, so a Safari-downloaded zip carries the
    # quarantine attr through to every .py file under src/ until it's
    # cleared. The CLI symlink at ~/.local/bin/okuro inherits its target's
    # attrs, so stripping the venv binary covers it without a separate call.
    xattr -dr com.apple.quarantine "$APP_DIR" 2>/dev/null || true
    xattr -dr com.apple.quarantine "$VENV/bin/okuro" 2>/dev/null || true
    xattr -dr com.apple.quarantine "$REPO_DIR" 2>/dev/null || true
    record_event "fact" "quarantine_strip" "ok" 0 "stripped from app, venv binary, repo dir"

    step ".app bundle at ${APP_DIR}"
    step "drag to /Applications if you want it system-wide"
    record_event "fact" "app_bundle" "ok" 0 "$APP_DIR"
    phase_done "bundle registered"
else
    step "skipping launcher on this platform"
    record_event "fact" "app_bundle" "warn" 0 "skipped (icon not present or unsupported OS)"
    phase_done "skipped"
fi

# ─── [8/11] CLI on PATH ────────────────────────────────────────────────────

phase "Terminal command" "symlink okuro into ~/.local/bin so you can run 'okuro doctor' from any terminal"

# ~/.local/bin is the XDG-standard user-scoped bin dir on both Linux and
# macOS. Symlink there instead of /usr/local/bin to avoid needing sudo and
# to respect the "no system-wide writes without explicit consent" default.
CLI_LINK_DIR="$HOME/.local/bin"
CLI_LINK="$CLI_LINK_DIR/okuro"
mkdir -p "$CLI_LINK_DIR"

# Overwrite any prior symlink (idempotent re-install) but refuse to replace
# a real file — that would almost certainly be user work we shouldn't touch.
if [ -L "$CLI_LINK" ] || [ ! -e "$CLI_LINK" ]; then
    ln -sf "$VENV/bin/okuro" "$CLI_LINK"
    step "symlink ${CLI_LINK} → ${VENV}/bin/okuro"
    record_event "fact" "cli_symlink" "ok" 0 "$CLI_LINK -> $VENV/bin/okuro"
elif [ -f "$CLI_LINK" ]; then
    step "existing non-symlink at ${CLI_LINK} — leaving it alone"
    record_event "fact" "cli_symlink" "warn" 0 "$CLI_LINK exists as regular file — not overwritten"
    warn_msg "to use 'okuro' from terminal, remove that file and re-run install.sh"
fi

# Check whether ~/.local/bin is already on PATH. POSIX-safe idiom — no Bash
# extensions — so this keeps working if the user's shell is dash or ash.
case ":$PATH:" in
    *":$CLI_LINK_DIR:"*)
        step "${CLI_LINK_DIR} is on PATH — 'okuro' works from any terminal"
        record_event "fact" "path_setup" "ok" 0 "$CLI_LINK_DIR on PATH"
        phase_done "okuro command is live"
        ;;
    *)
        # Derive the right rc file from the user's login shell. macOS default
        # is zsh (Catalina+); Linux default is bash. Fall back to a generic
        # hint if neither matches.
        case "${SHELL:-}" in
            */zsh)  RC_FILE="$HOME/.zshrc";  RC_NAME="~/.zshrc" ;;
            */bash)
                if [ "$UNAME" = "Darwin" ]; then
                    RC_FILE="$HOME/.bash_profile"; RC_NAME="~/.bash_profile"
                else
                    RC_FILE="$HOME/.bashrc"; RC_NAME="~/.bashrc"
                fi
                ;;
            *)      RC_FILE=""; RC_NAME="your shell rc file" ;;
        esac
        # Audit fix 2026-04-27: previous version printed "append this line"
        # and stopped — most users missed it and complained that `okuro` was
        # not on PATH after install. Auto-append to the right rc file when
        # we can identify it, marked with a fenced block so cmd_uninstall
        # can find and remove it cleanly. Print the line either way so the
        # user sees what we did.
        printf "      ${C_DIM}export PATH=\"\$HOME/.local/bin:\$PATH\"${C_RESET}\n"
        if [ -n "$RC_FILE" ]; then
            if [ -f "$RC_FILE" ] && grep -q ">>> okuro PATH >>>" "$RC_FILE" 2>/dev/null; then
                step "${CLI_LINK_DIR} block already present in ${RC_NAME} — leaving it"
                record_event "fact" "path_setup" "ok" 0 "okuro PATH block already in $RC_NAME"
            else
                {
                    printf '\n# >>> okuro PATH >>>\n'
                    printf '# Added by okuro install.sh; remove via `okuro uninstall` or by hand.\n'
                    printf 'export PATH="$HOME/.local/bin:$PATH"\n'
                    printf '# <<< okuro PATH <<<\n'
                } >> "$RC_FILE"
                step "added okuro PATH block to ${RC_NAME}"
                record_event "fact" "path_setup" "ok" 0 "appended okuro PATH block to $RC_NAME"
            fi
            warn_msg "open a NEW terminal (or run: source ${RC_NAME}) to pick up the PATH change"
            phase_done "okuro will be on PATH in any new terminal"
        else
            warn_msg "could not detect your shell rc file — append the line above by hand"
            record_event "fact" "path_setup" "warn" 0 "$CLI_LINK_DIR not on PATH; SHELL=${SHELL:-unknown}"
            phase_done "symlink ready — finish PATH setup to activate"
        fi
        ;;
esac

# ─── [9/11] environment summary ────────────────────────────────────────────

phase "Ready check" "quick summary of what's installed and where"

step "repo:    ${REPO_DIR}"
step "venv:    ${VENV}"
step "cli:     ${VENV}/bin/okuro"
step "shortcut: ${CLI_LINK} (if ~/.local/bin on PATH)"
step "launch:  okuro decides wizard vs dashboard and prints the actual URL"
phase_done "all systems go"

# ─── [10/11] verification ───────────────────────────────────────────────────
# Probe filesystem reality so the install log records what actually landed,
# not just what the install steps claimed. Every check writes a record_check
# entry to the jsonl stream. CHECK_PASS / CHECK_FAIL counters drive the
# summary line — `installed in 47s · verification: 7 ok, 0 failed`.

phase "Verification" "probing filesystem reality — what actually got installed"

# 1. Venv binary exists and is executable.
if [ -x "$VENV/bin/okuro" ]; then
    record_check "venv_okuro_binary" "ok" "$VENV/bin/okuro"
else
    record_check "venv_okuro_binary" "fail" "missing or not executable: $VENV/bin/okuro"
fi

# 2. CLI symlink points where we think it does.
if [ -L "$CLI_LINK" ]; then
    LINK_TARGET="$(readlink "$CLI_LINK" 2>/dev/null || echo '')"
    if [ "$LINK_TARGET" = "$VENV/bin/okuro" ]; then
        record_check "cli_symlink" "ok" "$CLI_LINK -> $LINK_TARGET"
    else
        record_check "cli_symlink" "warn" "$CLI_LINK -> $LINK_TARGET (expected $VENV/bin/okuro)"
    fi
elif [ -f "$CLI_LINK" ]; then
    record_check "cli_symlink" "warn" "$CLI_LINK exists as regular file — install left it alone"
else
    record_check "cli_symlink" "fail" "missing: $CLI_LINK"
fi

# 3. okuro module imports cleanly.
if "$VPY" -c "import okuro" 2>/dev/null; then
    OKURO_VER="$("$VPY" -c 'import okuro; print(okuro.__version__)' 2>/dev/null || echo unknown)"
    record_check "okuro_import" "ok" "okuro $OKURO_VER importable"
else
    record_check "okuro_import" "fail" "import okuro raised an error"
fi

# 4. macOS-specific reality checks.
if [ "$UNAME" = "Darwin" ]; then
    DARWIN_APP_DIR="$HOME/Applications/Okuro.app"
    if [ -d "$DARWIN_APP_DIR" ]; then
        record_check "app_bundle" "ok" "$DARWIN_APP_DIR"
        # Verify quarantine actually got stripped — the strip is the difference
        # between "first click works" and "cannot be opened because Apple
        # cannot verify it".
        if xattr "$DARWIN_APP_DIR" 2>/dev/null | grep -q quarantine; then
            record_check "app_quarantine" "fail" "com.apple.quarantine still present on $DARWIN_APP_DIR"
        else
            record_check "app_quarantine" "ok" "no quarantine attr on $DARWIN_APP_DIR"
        fi
    else
        record_check "app_bundle" "fail" "missing: $DARWIN_APP_DIR"
    fi

    # Logs directory should exist after the FIRST run of `okuro` (which
    # triggers service install via the wizard). Pre-wizard, this dir is
    # absent — record as warn rather than fail to avoid a false alarm on a
    # clean install.sh run.
    DARWIN_LOGS_DIR="$HOME/Library/Logs/okuro"
    if [ -d "$DARWIN_LOGS_DIR" ]; then
        record_check "logs_dir" "ok" "$DARWIN_LOGS_DIR"
    else
        record_check "logs_dir" "warn" "not created yet — appears after first 'okuro' run"
    fi

    # launchd plists land here only after the wizard completes (services
    # install runs at end of /onboarding/complete). Pre-wizard: 0 plists,
    # which is expected.
    PLIST_COUNT=0
    for p in "$HOME/Library/LaunchAgents/"com.okuro.*.plist; do
        [ -e "$p" ] && PLIST_COUNT=$((PLIST_COUNT + 1))
    done
    if [ "$PLIST_COUNT" -gt 0 ]; then
        record_check "launchd_plists" "ok" "$PLIST_COUNT plist(s) under ~/Library/LaunchAgents"
    else
        record_check "launchd_plists" "warn" "0 plists yet — installed by the wizard"
    fi

    # Verify pyobjc actually imports — pip install can land without these
    # if the wheel cache is stale. WEBVIEW_OK already captured this for the
    # phase log; double-check here so the verification record is independent.
    if "$VPY" -c "import WebKit; import Cocoa" 2>/dev/null; then
        record_check "pyobjc_bindings" "ok" "WebKit + Cocoa importable"
    else
        record_check "pyobjc_bindings" "warn" "missing — webview will fall back to default browser"
    fi
fi

# 5. Linux-specific reality checks.
if [ "$UNAME" = "Linux" ]; then
    LINUX_DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/okuro.desktop"
    if [ -f "$LINUX_DESKTOP" ]; then
        record_check "desktop_file" "ok" "$LINUX_DESKTOP"
    else
        record_check "desktop_file" "warn" "not created (icon source missing or skipped)"
    fi
    if [ -n "${WEBKIT_VER:-}" ]; then
        record_check "webkit_gtk" "ok" "$WEBKIT_VER"
    else
        record_check "webkit_gtk" "warn" "missing — webview will fall back to default browser"
    fi

    # Service backend reality check (audit Sprint 3E #28). When systemctl
    # is missing — Alpine, WSL1, Devuan, bare Docker — the wizard's
    # service-install path can't register systemd-user units. Instead of
    # crashing, okuro now falls back to ForegroundManager which writes a
    # tmux/nohup recipe to ~/.okuro/foreground/recipes/. We surface the
    # path so the user knows where to look on first run.
    if command -v systemctl >/dev/null 2>&1; then
        record_check "service_backend" "ok" "systemd-user (systemctl on PATH)"
    else
        FOREGROUND_RECIPES="$HOME/.okuro/foreground/recipes"
        record_check "service_backend" "warn" \
            "systemctl missing — okuro will run in foreground via the recipe at $FOREGROUND_RECIPES/<service>.sh"
    fi
fi

# 6. PATH check — re-evaluate at verification time so the user knows if a
# reload of their shell is needed before `okuro` works in a fresh terminal.
case ":$PATH:" in
    *":$CLI_LINK_DIR:"*) record_check "path_active" "ok" "$CLI_LINK_DIR on PATH" ;;
    *)                    record_check "path_active" "warn" "$CLI_LINK_DIR not on PATH (open a new shell after editing rc)" ;;
esac

phase_done "checks: ${CHECK_PASS} ok / ${CHECK_FAIL} failed"

# ─── [11/11] launch ────────────────────────────────────────────────────────

phase "Launch okuro" "starts the wizard on fresh installs, the dashboard on returning runs"
summary

# Mark the run as finished BEFORE we exec — the exec replaces this process
# so any code after wouldn't run. The summary call above already wrote the
# install_finished event. Flush log buffers so install.log + install.jsonl
# are durable on disk even though the exec immediately replaces this PID.
_flush_logs

# ─── returning install: converge, don't relaunch the wizard ────────────────
# CLASS: a recovery path built out of an installer that assumes a fresh
# machine. Everything above is scoped to "nothing was here before" — no
# migration (nothing to migrate on a fresh box), no service install (the
# wizard does that), and a final exec that lands a returning user on the
# dashboard, which never migrates. The lineage-swap re-clone in
# scripts/update.sh hands off here with --reinstall on a box that is
# emphatically NOT fresh: ~/.okuro holds a database at the OLD schema and
# service units whose ExecStart names a libexec/ this new venv does not have.
# Measured 2026-09-09: on Linux that is 203/EXEC, no daemon, no boot-time
# migration, and nothing tells the user.
#
# So when data already exists, finish the way a finished wizard would:
# update.sh --no-pull owns exactly that sequence (migrate → units → start →
# canon deploy → doctor) and is the one place it is maintained. Windows
# already converges — install.ps1 ends in `okuro init`, which migrates
# unconditionally — so this closes Linux and macOS to the same state.
OKURO_DATA_HOME="${OKURO_HOME:-$HOME/.okuro}"
if [ "$SKIP_AUTO_ROUTE" = "1" ] && [ -f "$OKURO_DATA_HOME/okuro.db" ] && [ -x "$REPO_DIR/scripts/update.sh" ]; then
    printf '[install] existing data at %s — converging via scripts/update.sh (migrate → services → doctor)\n' "$OKURO_DATA_HOME"
    export OKURO_UPDATE_REINSTALL=1
    exec "$REPO_DIR/scripts/update.sh" --no-pull
fi
exec "$VENV/bin/okuro" "$@"
