#!/usr/bin/env bash
# Pull latest okuro, build what's changed, restart what needs it, verify.
# Cross-platform: restarts route through `okuro service`, which knows
# systemd-user (Linux) and launchd (macOS).
#
# Usage:
#   scripts/update.sh             # full update
#   scripts/update.sh --dry-run   # print actions, change nothing
#   scripts/update.sh --no-pull   # skip git pull (use current HEAD)
#   scripts/update.sh --force     # allow uncommitted source changes

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DRY=0
DO_PULL=1
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        --no-pull) DO_PULL=0 ;;
        --force)   FORCE=1 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# Resolve OKURO + PY across layouts: repo-local .venv (Linux dev),
# repo-local venv/, or external install (pipx on Mac, etc.). For external
# editable installs we derive PY from the okuro shebang. OKURO_PY env-var
# overrides everything.
PY=""
OKURO=""
for cand in "$REPO_ROOT/.venv" "$REPO_ROOT/venv"; do
    if [[ -x "$cand/bin/okuro" ]]; then
        OKURO="$cand/bin/okuro"
        PY="$cand/bin/python"
        break
    fi
done
if [[ -z "$OKURO" ]] && command -v okuro >/dev/null 2>&1; then
    OKURO="$(command -v okuro)"
    if [[ -r "$OKURO" ]]; then
        shebang_py="$(head -n1 "$OKURO" 2>/dev/null | sed -n 's|^#!\(.*\)$|\1|p')"
        [[ -x "$shebang_py" ]] && PY="$shebang_py"
    fi
fi
PY="${OKURO_PY:-$PY}"
[[ -n "$OKURO" ]] || { echo "[update] okuro binary not found (.venv/, venv/, PATH)" >&2; exit 1; }
echo "[update] using okuro=$OKURO  python=${PY:-<none>}"

run() {
    if [[ "$DRY" == "1" ]]; then echo "[dry-run] $*"
    else echo "[update] $*"; "$@"
    fi
}

# Build the frontend SPA in place. Single source of truth: source code
# in git, dist built per-machine on update. Repo is private — we deliberately
# avoid CI artifact distribution to keep the install path zero-auth for
# Node-equipped users (Node is already a hard install dep for the wizard).
#
# pnpm acquisition (volta / corepack / npm -g) lives in build-frontend.sh, not
# here. This file used to carry its own ensure_pnpm copy; it went unused when
# the build moved to the shared builder, and a stale duplicate of a cascade
# this fiddly is worse than none — the corepack cold-cache hang was fixed in
# build-frontend.sh, and a copy here would have been the obvious wrong place
# to fix it next time.

build_fe_locally() {
    # Delegates to the shared scripts/build-frontend.sh — the single source of
    # truth for the SPA build (also used by install.sh's fresh path). Keeping
    # exactly one builder is what stops a fresh install from shipping no dist.
    run bash "$REPO_ROOT/scripts/build-frontend.sh" "$REPO_ROOT/src/okuro/web/frontend" || return 1
    mkdir -p "$DIST"
    echo "$HEAD_FULL" > "$DIST_SHA_FILE"
}

# ── pre-flight ────────────────────────────────────────────────────────
# Refuse to run on a dirty source tree (web/dist rebuild artifacts tolerated).
if ! git diff --quiet -- ':!src/okuro/web/dist' && [[ "$FORCE" != "1" ]]; then
    echo "[update] uncommitted source changes — pass --force to proceed." >&2
    git status --short -- ':!src/okuro/web/dist'
    exit 1
fi

OLD_HEAD="$(git rev-parse HEAD)"

# ── fetch FIRST — nothing is stopped until upstream is known reachable ───
# CLASS: an irreversible preparatory step taken before the operation is known
# to be viable. The quiesce below stops every service; if the fetch then fails
# (remote private, renamed, offline) the script dies under `set -e` with the
# stack down and no release involved. Measured 2026-09-09 against an
# unreachable remote: three `service stop`, one backup, then exit 128 and no
# restart hint. So: fetch, then quiesce, then decide. The lineage check itself
# stays below — it needs only the fetched ref, not a running service.
UPSTREAM=""
UPSTREAM_REMOTE=""
if [[ "$DO_PULL" == "1" ]]; then
    CUR_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
    UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    if [[ -z "$UPSTREAM" && "$CUR_BRANCH" != "HEAD" ]]; then
        UPSTREAM="origin/$CUR_BRANCH"
    fi
    if [[ -n "$UPSTREAM" ]]; then
        UPSTREAM_REMOTE="${UPSTREAM%%/*}"
        if [[ "$DRY" == "1" ]]; then
            # A fetch writes refs into .git, and a dry run must change nothing.
            # Check against the last-fetched ref instead, and say so.
            echo "[dry-run] git fetch $UPSTREAM_REMOTE (skipped — checking last-fetched $UPSTREAM)"
        else
            run git fetch "$UPSTREAM_REMOTE"
        fi
    fi
fi

# ── abort handling — never leave the stack down without saying so ────────
# Installed before the quiesce so every later failure passes through it.
# Before any code or schema has changed, an abort simply restarts what was
# stopped: the box is exactly as it was. After changes begin, the services
# stay down (old units on new code is not a state to boot into) and the
# restart command is printed — previously it was printed on two failure
# paths of ~fifteen. `exec` replaces this process, so a successful handoff
# to install.sh or the re-exec'd update.sh never trips this.
RESTART_HINT="okuro service start okuro-embed okuro-daemon okuro-orchestrator"
UPDATE_STAGE="pre-change"
_on_exit() {
    local rc=$?
    [[ "$rc" -eq 0 ]] && return 0
    [[ "$DRY" == "1" ]] && return 0
    [[ "${OKURO_UPDATE_QUIESCED:-0}" == "1" ]] || return 0
    if [[ "$UPDATE_STAGE" == "pre-change" ]]; then
        echo "[update] aborted before any code or schema changed — restarting the stopped services" >&2
        for svc in okuro-embed okuro-daemon okuro-orchestrator; do
            "$OKURO" service start "$svc" 2>/dev/null || true
        done
    else
        echo "[update] aborted after changes began — services left stopped. Restart with:" >&2
        echo "[update]   $RESTART_HINT" >&2
    fi
}
trap _on_exit EXIT

# ── quiesce + backup (EVERY update, before any code/schema change) ─────
# Stop the DB-writer services first so no stale process holds okuro.db open
# or races the new code/schema, THEN snapshot the current state. The window
# clients (desktop app / browser) are API consumers, not DB writers — they
# reconnect after restart; stopping the services is the real guarantee.
# Guarded so the self-reexec after a pull doesn't repeat it (env is inherited
# across exec). A failed backup ABORTS the update — never update without one.
if [[ "${OKURO_UPDATE_QUIESCED:-0}" != "1" ]]; then
    echo "[update] stopping okuro services (clean shutdown — no stale code mid-update)"
    for svc in okuro-orchestrator okuro-daemon okuro-embed; do
        run "$OKURO" service stop "$svc" 2>/dev/null || true
    done
    echo "[update] backing up current state (DB + keyring) before update…"
    if ! run "$OKURO" backup create --label pre-update; then
        echo "[update] BACKUP FAILED — aborting. Your data is untouched." >&2
        exit 1
    fi
    export OKURO_UPDATE_QUIESCED=1
    # We just took a full pre-update backup, so tell `okuro migrate` (below) to
    # skip its own premig snapshot — otherwise a multi-GB DB is copied twice.
    export OKURO_SKIP_PREMIG_SNAPSHOT=1
fi

# ── lineage-swap detection ────────────────────────────────────────────
# CLASS: a ff-only distribution channel whose upstream lineage can be
# legitimately REPLACED. okuro ships as `git clone` + `git pull --ff-only`, so
# when the public repo's history is regenerated (a release lineage with its own
# root commit), every clone made under the old history can never fast-forward
# again — the pull fails forever and the user has no path forward.
#
# The signal is the ABSENCE OF A MERGE BASE between HEAD and the fetched
# upstream. A root-commit mismatch is a subset of that. A plain divergence
# (shared merge base, local commits ahead) is NOT a swap: that is a real
# conflict only the user can resolve, and it must keep failing exactly as it
# did before.
#
# The migration is safe because of the infrastructure/data cut: ~/.okuro holds
# ALL user state (DB, keyring, config, roles, guard) and the repo directory
# holds only code plus regenerable build output. Re-cloning the repo therefore
# loses nothing of the user's.
lineage_swapped() {
    local upstream="$1" rc=0
    git rev-parse --verify --quiet "${upstream}^{commit}" >/dev/null 2>&1 || return 1
    git merge-base HEAD "$upstream" >/dev/null 2>&1 || rc=$?
    # rc=0 shared history · rc=1 no merge base (swap) · rc=128 bad ref (not our call)
    [[ "$rc" == "1" ]]
}

# Full copy of the okuro home beside it, before anything moves: every entry
# except regenerable caches and the backups. `okuro backup create` below is
# the consistent DATABASE copy; this is everything else, taken by the one
# piece of code that does not depend on which okuro version is installed.
# Unaffordable or failed → abort, nothing changed.
snapshot_okuro_home() {   # <dest>
    local src="${OKURO_HOME:-$HOME/.okuro}" dest="$1"
    [[ -d "$src" ]] || { echo "[update] no $src yet — nothing to snapshot"; return 0; }
    local ex="backups .deploy-backups hf models comfyui cortex prism-cache cache webview voice-previews repos proof logs"
    local need kb e
    need="$(du -sk "$src" 2>/dev/null | cut -f1)"
    for e in $ex; do
        [[ -e "$src/$e" ]] && { kb="$(du -sk "$src/$e" 2>/dev/null | cut -f1)"; need=$((need - ${kb:-0})); }
    done
    local free; free="$(df -Pk "$(dirname "$dest")" | awk 'NR==2{print $4}')"
    if [[ "${free:-0}" -le $((need + need / 10)) ]]; then
        echo "[update] not enough disk for a full snapshot of $src (need ~$((need / 1024)) MB, $((${free:-0} / 1024)) MB free) — aborting, nothing changed" >&2
        exit 1
    fi
    if [[ "$DRY" == "1" ]]; then echo "[dry-run] would: full copy of $src → $dest"; return 0; fi
    mkdir -p "$dest"
    local args=(); for e in $ex; do args+=("--exclude=./$e"); done
    if ! ( cd "$src" && tar -cf - "${args[@]}" . ) | ( cd "$dest" && tar -xpf - ); then
        echo "[update] snapshot to $dest failed — aborting, nothing changed" >&2
        exit 1
    fi
    echo "[update] full copy of $src kept at $dest (everything but caches; delete it once the new install checks out)"
}

# Replace this checkout with a fresh clone of the new lineage, then hand the
# rest of the job to the new copy's installer. Never returns.
reclone_after_lineage_swap() {
    local upstream="$1" remote="$2"
    local branch="${upstream#*/}"
    local remote_url; remote_url="$(git remote get-url "$remote")"
    local ts; ts="$(date +%Y%m%d-%H%M%S)"
    local staging="${REPO_ROOT}.reclone-${ts}"
    local retired="${REPO_ROOT}.old-${ts}"

    echo
    echo "[update] UPSTREAM LINEAGE REPLACED — $upstream shares no history with your checkout."
    echo "[update] A fast-forward is impossible and always will be, so this checkout is"
    echo "[update] migrated by re-cloning. Your data lives in ~/.okuro, not here — the repo"
    echo "[update] directory is code plus build output the installer regenerates."

    if [[ "$DRY" == "1" ]]; then
        echo "[dry-run] would: full copy of ${OKURO_HOME:-$HOME/.okuro} → ${OKURO_HOME:-$HOME/.okuro}.pre-reclone-${ts}"
        echo "[dry-run] would: $OKURO backup create --label pre-reclone"
        echo "[dry-run] would: git clone --branch $branch $remote_url $staging"
        echo "[dry-run] would: mv $REPO_ROOT $retired  &&  mv $staging $REPO_ROOT"
        echo "[dry-run] would: exec $REPO_ROOT/install.sh --reinstall"
        echo "[dry-run] nothing was changed. Re-run without --dry-run to migrate."
        exit 0
    fi

    snapshot_okuro_home "${OKURO_HOME:-$HOME/.okuro}.pre-reclone-${ts}"

    # Same backup machinery as the quiesce step, own label so the recovery
    # snapshot is identifiable in `okuro backup list`. Services are already
    # stopped (quiesce above, or the parent process before a self-reexec).
    # A failed backup ABORTS — nothing is moved, nothing is lost.
    echo "[update] backing up current state before re-clone…"
    if ! run "$OKURO" backup create --label pre-reclone; then
        echo "[update] BACKUP FAILED — aborting re-clone. Your checkout and data are untouched." >&2
        exit 1
    fi

    if [[ -e "$staging" ]]; then
        echo "[update] staging path already exists: $staging" >&2
        echo "[update] remove it and re-run — refusing to overwrite it." >&2
        exit 1
    fi
    echo "[update] cloning the new lineage from $remote_url ($branch)…"
    if ! run git clone --branch "$branch" "$remote_url" "$staging"; then
        echo "[update] clone FAILED — nothing was changed; your checkout is intact." >&2
        exit 1
    fi

    # Two renames within one parent directory — each is a single rename(2), so
    # the worst an interrupted swap can leave behind is a recoverable sibling.
    # Move out of the tree first: our cwd is about to be renamed away.
    cd "$(dirname "$REPO_ROOT")"
    if ! run mv "$REPO_ROOT" "$retired"; then
        echo "[update] could not move the old checkout aside — nothing changed." >&2
        exit 1
    fi
    if ! run mv "$staging" "$REPO_ROOT"; then
        echo "[update] could not move the new clone into place — restoring the old checkout." >&2
        mv "$retired" "$REPO_ROOT" || \
            echo "[update] RESTORE FAILED — your old checkout is at $retired" >&2
        exit 1
    fi
    cd "$REPO_ROOT"

    echo "[update] old checkout kept at: $retired"
    echo "[update]   Nothing there is needed: .venv and src/okuro/web/dist are both"
    echo "[update]   regenerated by the installer, and neither is tracked in git."
    echo "[update]   Delete it yourself once the new install checks out."
    echo "[update] handing off to the new checkout's installer…"
    # install.sh, not update.sh: the fresh clone has no .venv (gitignored, never
    # in git) and the old one moved away with the retired directory, so
    # update.sh's okuro/python resolution would find nothing or a dead
    # interpreter. --reinstall takes the full installer path — venv, editable
    # install, UI build, services — and never touches ~/.okuro. Same shape as
    # the self-update re-exec below: exec into the new copy by absolute path.
    exec bash "$REPO_ROOT/install.sh" --reinstall
}

# ── check lineage → merge ─────────────────────────────────────────────
# `git pull --ff-only` is split into its two halves: the fetch ran before the
# quiesce (so an unreachable remote stops nothing), the lineage check sits
# here, and the merge is the same operation with the same abort semantics on
# a real divergence.
if [[ "$DO_PULL" == "1" ]]; then
    if [[ -z "$UPSTREAM" ]]; then
        # Detached HEAD or no upstream — no lineage to compare. Leave the
        # original behaviour in place and let git report the problem.
        run git pull --ff-only
    else
        # Fetched above, before the quiesce. Only the decision is left.
        if lineage_swapped "$UPSTREAM"; then
            reclone_after_lineage_swap "$UPSTREAM" "$UPSTREAM_REMOTE"  # never returns
        fi

        run git merge --ff-only "$UPSTREAM"
    fi
fi

NEW_HEAD="$(git rev-parse HEAD)"
HEAD_FULL="$NEW_HEAD"
HEAD_SHORT="$(git rev-parse --short HEAD)"

# Self-update: if scripts/update.sh itself changed in this pull, re-exec
# the new version. The bash interpreter loaded the OLD script into memory
# before pull; without re-exec, downstream logic runs from the stale copy.
if [[ "$OLD_HEAD" != "$NEW_HEAD" ]] \
   && git diff --name-only "$OLD_HEAD" "$NEW_HEAD" | grep -qE '^scripts/update\.sh$'; then
    echo "[update] scripts/update.sh changed in pull — re-execing new version"
    exec bash "$REPO_ROOT/scripts/update.sh" --no-pull \
        $([[ "$DRY"   == "1" ]] && echo "--dry-run") \
        $([[ "$FORCE" == "1" ]] && echo "--force")
fi

# ── detect what changed ───────────────────────────────────────────────
CHANGED=""
if [[ "$OLD_HEAD" != "$NEW_HEAD" ]]; then
    CHANGED="$(git diff --name-only "$OLD_HEAD" "$NEW_HEAD")"
    echo "[update] $(printf '%s\n' "$CHANGED" | wc -l | tr -d ' ') file(s) changed between $OLD_HEAD and $NEW_HEAD"
fi

deps_changed=0
fe_changed=0
py_changed=0
mig_changed=0
units_changed=0
while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    case "$f" in
        pyproject.toml|requirements*.txt|setup.cfg|setup.py)         deps_changed=1 ;;
        src/okuro/web/frontend/*)                                     fe_changed=1 ;;
        src/okuro/db/migrations/*)                                    mig_changed=1 ;;
        src/okuro/system/services/*|*.service|*.plist)                units_changed=1 ;;
    esac
    [[ "$f" =~ ^src/okuro/.*\.py$ ]] && py_changed=1
done <<< "$CHANGED"

echo "[update] deps:$deps_changed  fe:$fe_changed  py:$py_changed  migrations:$mig_changed  units:$units_changed"

# From here on the box is no longer as it was (new code, new deps, new
# schema): an abort must leave services stopped and say how to restart.
UPDATE_STAGE="changed"

# ── apply: backend deps ───────────────────────────────────────────────
# Always re-sync the editable install — NOT just when THIS pull touched
# pyproject. `deps_changed` only sees the current pull's diff, so a dependency
# added in an EARLIER pull that never re-installed (or a pull whose pip step
# failed) leaves the env silently short a package. That exact drift shipped
# `pathspec` missing on macOS and made the cortex MCP module fail to import
# (installability audit 2026-06-05: "△ Cortex Index: No module named
# 'pathspec'"). pip is a fast no-op when everything is already satisfied, so
# running it every update is cheap insurance against import-time breakage.
if [[ -n "$PY" && -x "$PY" ]]; then
    if [[ "$deps_changed" == "1" ]]; then
        echo "[update] dependencies changed — reinstalling editable package"
    else
        echo "[update] re-syncing editable install (dep-drift guard)"
    fi
    # Include the voice extra so dictation STT (faster-whisper) survives updates
    # — a plain `-e .` drops optional extras and would silently re-mute dictation.
    run "$PY" -m pip install -e ".[voice]" --quiet
elif [[ "$deps_changed" == "1" ]]; then
    echo "[update] pyproject.toml changed but no writable venv python detected."
    echo "         pipx users:    pipx reinstall okuro"
    echo "         editable elsewhere: OKURO_PY=/path/to/venv/bin/python scripts/update.sh"
fi

# ── apply: frontend dist (independent of HEAD movement) ───────────────
# dist/ is gitignored; built locally per-machine. Rebuild iff: FE source
# changed in this pull, dist is missing, or dist's commit-pin doesn't
# match HEAD (e.g. previous build interrupted, branch switch, fresh clone).
DIST="$REPO_ROOT/src/okuro/web/dist"
DIST_SHA_FILE="$DIST/.commit-sha"
NEED_FE=0
NEED_FE_REASON=""
if [[ "$fe_changed" == "1" ]]; then
    NEED_FE=1; NEED_FE_REASON="FE source changed"
elif [[ ! -f "$DIST/index.html" ]]; then
    NEED_FE=1; NEED_FE_REASON="dist missing"
elif [[ ! -f "$DIST_SHA_FILE" ]] || [[ "$(cat "$DIST_SHA_FILE" 2>/dev/null)" != "$HEAD_FULL" ]]; then
    NEED_FE=1; NEED_FE_REASON="dist commit-pin mismatch (HEAD=$HEAD_SHORT)"
fi

if [[ "$NEED_FE" == "1" ]]; then
    echo "[update] FE rebuild needed — $NEED_FE_REASON"
    if [[ "$DRY" == "1" ]]; then
        echo "[dry-run] would run pnpm install + pnpm build"
    elif ! build_fe_locally; then
        echo "[update] FE build FAILED — dashboard may be stale" >&2
    fi
fi

if [[ "$units_changed" == "1" ]]; then
    echo "[update] service unit files changed — re-run 'okuro service install <name>' manually if expected."
fi

# ── apply DB migrations (services are stopped — no race) ──────────────
# Always run: `okuro migrate` is a no-op when current and catches drift from
# an earlier pull that never migrated. A pre-update backup already exists
# (quiesce step above), so a failed migration is recoverable via
# `okuro backup restore latest`.
echo "[update] applying DB migrations…"
if ! run "$OKURO" migrate; then
    echo "[update] migrate FAILED — services left stopped. Recover with:" >&2
    echo "[update]   okuro backup restore latest   # rolls back DB to the pre-update snapshot" >&2
    echo "[update] then re-run update once the cause is fixed." >&2
    exit 1
fi

# ── reclaim vec_cortex bloat (services stopped — exclusive DB access) ──
# A NULL partition key historically bloated vec_cortex ~1024x (one near-empty
# chunk per unscoped vector — a 25GB index on a barely-used box). The insert
# fix prevents NEW bloat; this reclaims an already-bloated index on upgrade.
# Gated inside `reclaim`: it only repacks when the bloat ratio is high
# (needs_repack), so this is a cheap no-op on a healthy DB. Runs here while
# services are stopped so VACUUM has exclusive access. Best-effort — never
# abort an update over index hygiene (a pre-update backup already exists).
echo "[update] checking vec_cortex for bloat (auto-repack if needed)…"
run "$OKURO" cortex reclaim --repack --no-reconcile || \
    echo "[update] cortex reclaim skipped/failed (non-fatal) — run 'okuro cortex reclaim --repack' manually if okuro.db is large." >&2

# ── start services on the new code + migrated schema ──────────────────
# We stopped everything during quiesce, so always start (clean boot — no
# stale code). Order: embed → daemon (consumes embed) → orchestrator (serves).
# We start embed FIRST and on its own, auto-heal vec_* dims while it is the
# only service up, then start the consumers — so nothing writes a vec_* table
# mid-rebuild.
# ── re-install service units when the venv is fresh ──────────────────
# install.sh --reinstall on a box that already has data hands off here with
# OKURO_UPDATE_REINSTALL=1. The units may already exist, but their ExecStart
# names <venv>/libexec/<service>, which only `service install` materialises
# (via get_service_registry → ensure_okuro_interpreter). A fresh venv has no
# libexec/, so without this step systemd reports 203/EXEC and the daemon —
# and with it the boot-time migration — never runs. Measured 2026-09-09.
if [[ "${OKURO_UPDATE_REINSTALL:-0}" == "1" ]]; then
    echo "[update] fresh venv — re-installing service units so ExecStart points at the new interpreter"
    for svc in okuro-embed okuro-daemon okuro-orchestrator; do
        run "$OKURO" service install "$svc" --no-start --enable || \
            echo "[update] service install $svc failed (non-fatal) — okuro doctor will report it" >&2
    done
fi

echo "[update] starting okuro-embed"
run "$OKURO" service start okuro-embed

# ── auto-heal vec_* dimensions (re-embed tables drifted from the tier) ─
# `okuro migrate` runs with services stopped, so its post-migrate pass can
# only realign EMPTY vec tables (ensure_vec_dims empty_only=True). Populated
# tables built at an old embedding dim — e.g. legacy 384d memory/thoughts/
# roles after a model swap — are deferred there because re-embedding needs the
# embed service. Now that okuro-embed is up, run the full realign: any vec
# table whose stored dim != the active tier dim is dropped, recreated at the
# right dim, and re-embedded from its source rows. Idempotent — a healthy DB
# skips every table. Gated on embed readiness so a slow/failed embed boot can
# never drop+empty a populated table (the very risk migrate guards against).
if [[ "$DRY" == "1" ]]; then
    echo "[dry-run] would wait for okuro-embed, then: ${PY:-python} -m okuro.embed.repair --quiet"
elif [[ -n "$PY" ]]; then
    EMBED_PORT="$("$PY" -c 'from okuro.system.port_registry import embed_port; print(embed_port())' 2>/dev/null || echo 13334)"
    EMBED_URL="http://${OKURO_EMBED_HOST:-127.0.0.1}:${EMBED_PORT}/health"
    echo "[update] waiting for okuro-embed model load before vector auto-heal…"
    embed_ready=0
    for _ in $(seq 1 60); do
        if curl -fsS --max-time 2 "$EMBED_URL" 2>/dev/null | grep -qE '"loaded": *true'; then
            embed_ready=1; break
        fi
        sleep 2
    done
    if [[ "$embed_ready" == "1" ]]; then
        echo "[update] auto-healing vec_* dimensions (re-embed drifted tables)…"
        "$PY" -m okuro.embed.repair --quiet || \
            echo "[update] vector auto-heal failed (non-fatal) — run '$PY -m okuro.embed.repair' once embed is healthy." >&2
    else
        echo "[update] okuro-embed not ready in time — skipped vector auto-heal." >&2
        echo "[update]   heal manually later with: $PY -m okuro.embed.repair" >&2
    fi
else
    echo "[update] no venv python resolved — skipped vector auto-heal (run okuro.embed.repair manually)." >&2
fi

# Consumers start now that vec_* dims are aligned.
echo "[update] starting okuro-daemon + okuro-orchestrator"
for svc in okuro-daemon okuro-orchestrator; do
    run "$OKURO" service start "$svc"
done

# ── re-register okuro's agent surface ─────────────────────────────────
# MCP registration is the one surface the daemon's refresh never touches, so
# an update must re-run it: this strips the ~/.mcp.json shadow that caused the
# every-session disconnect, fixes any consumer whose config the app rewrote,
# and picks up consumers installed since the last run.
#
# stdio, NOT auto: every consumer supports stdio, and it is what is already
# registered. `auto` would resolve to http once the daemon is up and write an
# http entry to EVERY consumer — but Claude Desktop and Antigravity have no
# Streamable HTTP transport, so that mis-registers them. Per-consumer transport
# (coerce http→stdio where unsupported) is registry-driven Phase B work; until
# then stdio is the safe, universal choice. Non-fatal — a registration hiccup
# must not fail an otherwise-good update; canon-drift-alarm keeps warning until
# it is resolved.
echo "[update] re-registering okuro's MCP surface (canon deploy)…"
run "$OKURO" canon deploy --transport stdio || \
    echo "[update] canon deploy reported an issue (non-fatal) — run 'okuro canon status' to inspect." >&2

# ── verify ────────────────────────────────────────────────────────────
echo
echo "[update] running okuro doctor…"
exec "$OKURO" doctor
