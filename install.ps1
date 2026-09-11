#Requires -Version 5.1
<#
okuro installer (Windows) -- PowerShell sibling of install.sh.

Creates a local .venv, pip-installs okuro in editable mode, builds the web
SPA, and launches the onboarding wizard. Re-runnable: an existing .venv is
reused.

The wizard's "semantic search" step installs the hardware-matched embed
extra (CPU/CUDA), and clicking Done installs the background services as
Task Scheduler tasks -- see
okuro.system.service_manager.WindowsServiceManager.

Usage:
  .\install.ps1
  .\install.ps1 init --no-browser   # forwarded to `okuro init`

# No param() / CmdletBinding: a plain script so trailing args (e.g.
# `init --no-browser`) land in $args and forward to the okuro CLI unchanged.
#>

$ErrorActionPreference = 'Stop'
$RepoDir = $PSScriptRoot
$Venv    = Join-Path $RepoDir '.venv'
$VPy     = Join-Path $Venv 'Scripts\python.exe'
$VOkuro  = Join-Path $Venv 'Scripts\okuro.exe'

function Step($m)   { Write-Host "-- $m --" }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# --- [1/5] locate Python 3.11+ ---------------------------------------------
Step "Locating Python 3.11+"
$pyExe = $null
$pyArgs = @()
$verCheck = "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)"
# Prefer the `py` launcher (it can target a specific version), then `python`.
if (Have 'py') {
    foreach ($v in '3.12', '3.11', '3') {
        & py "-$v" -c $verCheck 2>$null
        if ($LASTEXITCODE -eq 0) { $pyExe = 'py'; $pyArgs = @("-$v"); break }
    }
}
if (-not $pyExe -and (Have 'python')) {
    & python -c $verCheck 2>$null
    if ($LASTEXITCODE -eq 0) { $pyExe = 'python' }
}
if (-not $pyExe) {
    throw "No Python 3.11+ found. Install it (winget install Python.Python.3.12) or run bootstrap.ps1, then re-run."
}
Write-Host "  using: $pyExe $($pyArgs -join ' ')"

# --- [2/5] venv ------------------------------------------------------------
Step "Virtual environment (.venv)"
if (-not (Test-Path $VPy)) {
    Write-Host "  creating venv at $Venv"
    & $pyExe @pyArgs -m venv $Venv
    if (-not (Test-Path $VPy)) { throw "venv creation failed" }
} else {
    Write-Host "  reusing existing venv"
}
& $VPy -m pip install --quiet --upgrade pip

# --- [3/5] install okuro (editable) ----------------------------------------
# Base install only -- the wizard's semantic-search step adds the
# hardware-matched embed extra (torch) so the base stays lean.
Step "Installing okuro (pip install -e .)"
& $VPy -m pip install --progress-bar on -e $RepoDir
if ($LASTEXITCODE -ne 0) { throw "pip install failed -- see output above" }

# --- [4/5] build the dashboard SPA -----------------------------------------
# Mirrors scripts/build-frontend.sh's pnpm acquisition cascade (corepack ->
# npm). A missing UI build is non-fatal: pywebview falls back to the browser.
Step "Building the dashboard UI"
$Frontend = Join-Path $RepoDir 'src\okuro\web\frontend'
if (Test-Path (Join-Path $Frontend 'package.json')) {
    if (-not (Have 'pnpm')) {
        if (Have 'corepack') {
            Write-Host "  enabling pnpm via corepack"
            corepack enable pnpm 2>$null
        }
        if (-not (Have 'pnpm') -and (Have 'npm')) {
            Write-Host "  installing pnpm via npm"
            npm install -g pnpm
        }
    }
    if (Have 'pnpm') {
        Push-Location $Frontend
        try {
            pnpm install --frozen-lockfile
            pnpm build
        } finally { Pop-Location }
    } else {
        Write-Warning "no Node tooling (pnpm/corepack/npm) on PATH -- skipping UI build; the dashboard will fall back to the browser. Install Node and re-run for the desktop window."
    }
} else {
    Write-Warning "no frontend package.json at $Frontend -- skipping UI build"
}

# --- [5/5] launch the onboarding wizard ------------------------------------
Step "Launching the onboarding wizard"
if (-not (Test-Path $VOkuro)) {
    throw "okuro entrypoint missing at $VOkuro -- pip install may have failed"
}
if ($args.Count -gt 0) {
    & $VOkuro @args
} else {
    & $VOkuro init
}
