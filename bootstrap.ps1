#Requires -Version 5.1
<#
okuro bootstrap (Windows) -- zero-to-running install AND update with no
pre-existing checkout. PowerShell sibling of bootstrap.sh.

Ensures the toolchain via winget (Git, Python 3.12, Node.js, and the Edge
WebView2 runtime that pywebview's dashboard needs), then clones okuro (first
run) or updates it (subsequent runs) and hands off to install.ps1.

Idempotent + resumable: every step is skipped if already satisfied, so a
re-run after a network drop continues where it left off.

Usage:
  .\bootstrap.ps1            # install or update
  .\bootstrap.ps1 -Check     # report toolchain state, change NOTHING

Env overrides:
  OKURO_REPO_URL      git URL (default: public okuro-ai)
  OKURO_INSTALL_DIR   clone target (default: %USERPROFILE%\okuro-ai)
#>
[CmdletBinding()]
param([switch]$Check)

$ErrorActionPreference = 'Stop'

$RepoUrl    = if ($env:OKURO_REPO_URL)    { $env:OKURO_REPO_URL }    else { 'https://github.com/saxmode/okuro-ai.git' }
$InstallDir = if ($env:OKURO_INSTALL_DIR) { $env:OKURO_INSTALL_DIR } else { Join-Path $env:USERPROFILE 'okuro-ai' }
$Log        = Join-Path $env:USERPROFILE '.okuro-bootstrap.log'

function Log($m)  { $m | Tee-Object -FilePath $Log -Append | Out-Host }
function Step($m) { Log ''; Log "-- $m --" }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Test-WinGetPackage($id) {
    # winget list exits 0 and echoes the id when the package is installed.
    # --accept-source-agreements is required on the FIRST winget call of a
    # fresh box: without it winget blocks on an interactive "accept source
    # terms? [Y/N]" prompt that is invisible under `irm | iex`, so the whole
    # bootstrap appears to hang at the starting banner.
    $out = winget list --id $id -e --source winget --accept-source-agreements 2>$null
    return ($LASTEXITCODE -eq 0 -and ($out -match [regex]::Escape($id)))
}

function Install-WinGetPackage($id, $label) {
    if (Test-WinGetPackage $id) { Log "  $label already installed"; return }
    Step "Installing $label ($id)"
    # Pin --source winget: all okuro packages live in the winget community
    # source, and pinning sidesteps the msstore source when it is broken
    # (e.g. cert error 0x8a15005e on fresh VMs / clock skew). Without it,
    # an id present in two sources makes winget abort with "specify a source".
    winget install --id $id -e --source winget --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget install $id failed (exit $LASTEXITCODE)" }
}

# --- check mode -- report only, no mutations --------------------------------
if ($Check) {
    Write-Host "okuro bootstrap -- toolchain check (Windows)"
    foreach ($t in @('git', 'python', 'node', 'winget')) {
        if (Have $t) {
            $ver = (& $t --version 2>$null) -join ' '
        } else {
            $ver = 'MISSING'
        }
        Write-Host ("  {0,-16} {1}" -f $t, $ver)
    }
    if (Test-Path (Join-Path $InstallDir '.git')) {
        $state = "$InstallDir (update)"
    } else {
        $state = 'absent (fresh install)'
    }
    Write-Host ("  {0,-16} {1}" -f 'okuro checkout', $state)
    return
}

Set-Content -Path $Log -Value '' -ErrorAction SilentlyContinue
Step "okuro bootstrap starting (Windows) -- log: $Log"

if (-not (Have 'winget')) {
    throw "winget not found. Install 'App Installer' from the Microsoft Store (Windows 10 21H2+/11), then re-run."
}

# --- toolchain via winget --------------------------------------------------
Install-WinGetPackage 'Git.Git'                       'Git'
Install-WinGetPackage 'Python.Python.3.12'            'Python 3.12'
Install-WinGetPackage 'OpenJS.NodeJS'                 'Node.js'
Install-WinGetPackage 'Microsoft.EdgeWebView2Runtime' 'Edge WebView2 Runtime'

# winget extends PATH only for *new* shells. Refresh this process's PATH from
# the registry so git/python/node are visible to the rest of this run.
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$userPath    = [Environment]::GetEnvironmentVariable('Path', 'User')
$env:Path    = "$machinePath;$userPath"

if (-not (Have 'git')) {
    throw "git still missing after toolchain setup -- open a new terminal and re-run"
}

# --- clone or update, then hand off to install.ps1 -------------------------
# install.ps1 is re-runnable (reuses an existing .venv), so it serves both the
# fresh-install and the post-pull update path.
#
# CLASS (identical to scripts/update.sh): a ff-only distribution channel whose
# upstream lineage can be legitimately REPLACED. okuro ships by clone + ff-only
# pull, so when the public repo's history is regenerated -- a release lineage
# with its own root commit -- no clone made under the old history can ever
# fast-forward again. The signal is the ABSENCE OF A MERGE BASE between HEAD
# and the fetched upstream; a root-commit mismatch is a subset of that. A plain
# divergence (shared merge base) is NOT a swap and must keep failing as before.
# Migration is safe because %USERPROFILE%\.okuro holds ALL user state and the
# checkout holds only code plus regenerable build output.
#
# -Check returns above this block, so check mode never reaches any of it.
if (Test-Path (Join-Path $InstallDir '.git')) {
    Step "Updating okuro at $InstallDir"
    $lineageSwapped = $false
    $upstream = 'origin/main'
    Push-Location $InstallDir
    try {
        # pull is split into fetch + merge so the lineage check can sit between
        # the two halves. Same operation, same abort semantics on a real
        # divergence -- the check just needs the fetched ref first.
        git fetch origin
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed -- check your network" }

        $branch = (git rev-parse --abbrev-ref HEAD 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $branch -or $branch -eq 'HEAD') { $branch = 'main' }
        $upstream = "origin/$branch"

        git rev-parse --verify --quiet "$upstream^{commit}" > $null 2>&1
        if ($LASTEXITCODE -eq 0) {
            git merge-base HEAD $upstream > $null 2>&1
            # 0 = shared history, 1 = no merge base (the swap signal),
            # 128 = bad ref, which is not ours to interpret.
            if ($LASTEXITCODE -eq 1) { $lineageSwapped = $true }
        }

        if (-not $lineageSwapped) {
            git merge --ff-only $upstream
            if ($LASTEXITCODE -ne 0) {
                throw "git merge --ff-only failed -- local changes? run: cd `"$InstallDir`"; git status"
            }
            & powershell -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'install.ps1')
        }
    } finally { Pop-Location }

    # Outside the Push-Location: Windows will not rename the process's own
    # working directory.
    if ($lineageSwapped) {
        Step "Upstream lineage replaced -- migrating $InstallDir by re-clone"
        Log "  $upstream shares no history with your checkout, so a fast-forward is"
        Log "  impossible and always will be. Your data lives in %USERPROFILE%\.okuro,"
        Log "  not here -- this directory is code plus build output install.ps1 rebuilds."

        # Same backup contract as the Linux/macOS path: a failed backup ABORTS
        # and leaves the checkout untouched. No okuro.exe means nothing is
        # installed yet, so there is nothing to back up.
        $okuroExe = Join-Path $InstallDir '.venv\Scripts\okuro.exe'
        if (Test-Path $okuroExe) {
            Log "  backing up current state (DB + keyring) before re-clone..."
            & $okuroExe backup create --label pre-reclone
            if ($LASTEXITCODE -ne 0) {
                throw "backup FAILED -- aborting re-clone. Your checkout and data are untouched."
            }
        } else {
            Log "  no okuro.exe under $InstallDir -- nothing installed to back up, continuing"
        }

        $ts      = Get-Date -Format 'yyyyMMdd-HHmmss'
        $staging = "$InstallDir.reclone-$ts"
        $retired = "$InstallDir.old-$ts"
        if (Test-Path $staging) {
            throw "staging path already exists: $staging -- remove it and re-run"
        }

        Log "  cloning the new lineage from $RepoUrl"
        git clone $RepoUrl $staging
        if ($LASTEXITCODE -ne 0) {
            throw "git clone failed -- nothing was changed; your checkout is intact"
        }

        # Two renames inside one parent directory: the worst an interrupted
        # swap can leave behind is a recoverable sibling.
        Move-Item -LiteralPath $InstallDir -Destination $retired
        try {
            Move-Item -LiteralPath $staging -Destination $InstallDir
        } catch {
            Move-Item -LiteralPath $retired -Destination $InstallDir
            throw "could not move the new clone into place -- old checkout restored"
        }

        Log "  old checkout kept at: $retired"
        Log "  Nothing there is needed: .venv and the built UI are both regenerated by"
        Log "  install.ps1, and neither is tracked in git. Delete it yourself once the"
        Log "  new install checks out."
        Push-Location $InstallDir
        try {
            & powershell -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'install.ps1')
        } finally { Pop-Location }
    }
} else {
    Step "Installing okuro into $InstallDir"
    git clone $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed -- check your network" }
    Push-Location $InstallDir
    try {
        & powershell -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'install.ps1')
    } finally { Pop-Location }
}
